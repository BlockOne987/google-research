# coding=utf-8
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Exports xmap based transformer as an Australis library."""

import jax
from jax.experimental.australis import exporter
from jax.experimental.pjit import PartitionSpec as P
from jax.experimental.pjit import pjit
import jax.numpy as jnp
import numpy as np

from scaling_transformer_inference_efficiency import checkpoint
from scaling_transformer_inference_efficiency import chunk
from scaling_transformer_inference_efficiency import inference
from scaling_transformer_inference_efficiency import partitioning
from scaling_transformer_inference_efficiency import weights
from scaling_transformer_inference_efficiency.layers import two_d_parallel_xmap
from scaling_transformer_inference_efficiency.maps import shard_map

jax.config.update('jax_array', True)  # required for jax < 0.4.0

X, Y, Z = 2, 2, 2  # slice sizes pylint: disable = invalid-name


def setup(mesh, batch_size, seq_len = 32):
  """Sets up necessary inputs."""
  # assert len(jax.devices()) == X * Y * Z

  dtype = jnp.float32
  h = checkpoint.HParams(
      layers=8, embed=16, ff=32, heads=16, qkv=4, max_len=256, vocab=1024
  )

  params_logical = weights.Weights.logical_axes()
  chunk_logical = chunk.Chunk(tokens=P(None, None), lengths=P(None))

  params_sharding = jax.tree_util.tree_map(
      partitioning.logical_to_physical, params_logical
  )

  chunk_sharding = jax.tree_util.tree_map(
      partitioning.logical_to_physical, chunk_logical
  )

  result_logical = chunk.FullChunkResult.logical_axes()
  result_sharding = jax.tree_util.tree_map(
      partitioning.logical_to_physical, result_logical
  )

  def model_init():
    key = jax.random.PRNGKey(0)
    key, k2, k3, k4, k5 = jax.random.split(key, 5)
    q_wi = jax.random.normal(
        k2, (h.layers, h.heads, h.embed, h.q_wi_per_head), dtype
    )
    kv = jax.random.normal(k3, (h.layers, h.embed, 1, 2 * h.qkv), dtype)
    o_wo = jax.random.normal(
        k4, (h.layers, h.heads, h.o_wo_per_head, h.embed), dtype
    )
    embedding = jax.random.normal(k5, (h.vocab, h.embed), dtype)
    sin = jnp.ones((h.max_len, h.qkv // 2), dtype)
    cos = jnp.ones((h.max_len, h.qkv // 2), dtype)

    # create the params
    params_pjit = weights.Weights(
        weights.Layer(q_wi, kv, o_wo), sin, cos, embedding
    )

    # create the token inputs
    token_chunk = chunk.Chunk(
        tokens=jnp.reshape(
            jnp.arange(batch_size * seq_len), (batch_size, seq_len)
        ),
        lengths=jnp.array([seq_len] * batch_size),
    )
    return params_pjit, token_chunk

  with mesh:
    model_init_lowered = pjit(
        model_init, out_axis_resources=(params_sharding, chunk_sharding)
    ).lower()
    params_pjit_shape, token_chunk_shape = jax.eval_shape(model_init)

  kv_caches = []

  return (
      model_init_lowered,
      dtype,
      h,
      mesh,
      params_pjit_shape,
      params_pjit_shape,
      kv_caches,
      token_chunk_shape,
      chunk_sharding,
      params_sharding,
      result_sharding,
  )


def lower():
  """Uses the jax staging API to lower the init and fwd functions."""
  device_mesh = np.array(exporter.fake_devices(8, 'tpu')).reshape((2, 2, 2))
  mesh_axis_names = ('x', 'y', 'z')
  mesh = jax.experimental.maps.Mesh(device_mesh, mesh_axis_names)

  batch_unsharded = False
  attn_sharding = partitioning.AttnAllToAll.AXES_YZX
  rules = partitioning.PartitioningRules(
      partitioning.make_rules_two_d(
          attn_sharding, batch_unsharded=batch_unsharded
      )
  )

  with rules:
    (
        model_init_lowered,
        dtype,
        h,
        mesh,
        _,
        rotated_params,
        kv_caches,
        token_chunk,
        chunk_sharding,
        param_sharding,
        result_sharding,
    ) = setup(mesh, batch_size=8)

  def fwd(params, token_chunk):
    """Wraps the inference fn to ease shardmap in pytree definition."""
    return inference.infer_xmap(
        h,
        two_d_parallel_xmap.transformer_layer_weight_stationary,
        params,
        kv_caches,
        token_chunk,
        attn_all_to_all=attn_sharding,
        latency_collectives=False,
        shard_seqlen_vs_batch=False,
        batch_unsharded=batch_unsharded,
        intermediate_dtype=dtype,
    )

  with mesh:

    def pjit_fwd(rotated_params, token_chunk):
      return shard_map.shard_map(
          fwd,
          mesh,
          in_specs=(param_sharding, chunk_sharding),
          out_specs=result_sharding,
      )(rotated_params, token_chunk)

    result = pjit(
        pjit_fwd,
        in_axis_resources=(param_sharding, chunk_sharding),
        out_axis_resources=result_sharding,
    )
    xmap_transformer_fwd_lowered = result.lower(rotated_params, token_chunk)

  return [
      ('xmap_transformer_init', model_init_lowered),
      ('xmap_transformer_fwd', xmap_transformer_fwd_lowered),
  ]


if __name__ == '__main__':
  exporter.run(lower)
