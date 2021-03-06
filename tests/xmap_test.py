# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# flake8: noqa

import functools
import itertools
import os
import unittest
from unittest import SkipTest, skip, skipIf

import numpy as np
from absl.testing import absltest
from absl.testing import parameterized
from functools import partial

import jax
import jax.numpy as jnp
from jax import test_util as jtu
from jax import vmap
from jax import lax
from jax.experimental.maps import Mesh, mesh, xmap, A
from jax.lib import xla_bridge
from jax.util import curry, unzip2
from jax.interpreters import pxla

from jax.config import config
config.parse_flags_with_absl()

ignore_xmap_warning = functools.partial(
  jtu.ignore_warning, message=".*is an experimental.*")

# TODO(mattjj): de-duplicate setUpModule and tearDownModule with pmap_test.py
# Run all tests with 8 CPU devices.
def setUpModule():
  global prev_xla_flags
  prev_xla_flags = os.getenv("XLA_FLAGS")
  flags_str = prev_xla_flags or ""
  # Don't override user-specified device count, or other XLA flags.
  if "xla_force_host_platform_device_count" not in flags_str:
    os.environ["XLA_FLAGS"] = (flags_str +
                               " --xla_force_host_platform_device_count=8")
  # Clear any cached backends so new CPU backend will pick up the env var.
  xla_bridge.get_backend.cache_clear()

# Reset to previous configuration in case other test modules will be run.
def tearDownModule():
  if prev_xla_flags is None:
    del os.environ["XLA_FLAGS"]
  else:
    os.environ["XLA_FLAGS"] = prev_xla_flags
  xla_bridge.get_backend.cache_clear()


@curry
def with_mesh(named_shape, f):
  def new_f(*args, **kwargs):
    axis_names, shape = unzip2(named_shape)
    size = np.prod(shape)
    local_devices = list(jax.local_devices())
    if len(local_devices) < size:
      raise SkipTest(f"Test requires {size} local devices")
    mesh_devices = np.array(local_devices[:size]).reshape(shape)
    with mesh(mesh_devices, axis_names):
      return f(*args, **kwargs)
  return new_f

def use_spmd_lowering(f):
  def new_f(*args, **kwargs):
    if jtu.device_under_test() != "tpu":
      raise SkipTest
    jax.experimental.maps.make_xmap_callable.cache_clear()
    old = jax.experimental.maps.EXPERIMENTAL_SPMD_LOWERING
    jax.experimental.maps.EXPERIMENTAL_SPMD_LOWERING = True
    try:
      return f(*args, **kwargs)
    finally:
      jax.experimental.maps.EXPERIMENTAL_SPMD_LOWERING = old
  return new_f


class XMapTest(jtu.JaxTestCase):

  def setUp(self):
    if not config.omnistaging_enabled:
      raise SkipTest("xmap requires omnistaging")

  @ignore_xmap_warning()
  def testBasic(self):
    local_devices = list(jax.local_devices())
    if len(local_devices) < 4:
      raise SkipTest("Test requires at least 4 local devices")
    def f(a, b):
      return a * 2, b * 4
    devices = np.array(local_devices[:4]).reshape((2, 2))
    with mesh(devices, ('x', 'y')):
      fm = xmap(f,
                in_axes=[A({'a': 0, 'b': 1}), A({'c': 0})],
                out_axes=[A({'a': 0, 'b': 1}), A({'c': 0})],
                schedule=[
                  ('a', 'x'),
                  ('b', 'y'),
                  ('c', 'x'),
                  ('a', 'vectorize'),
                  ('b', 'vectorize'),
                ])
      ashape = (16, 8, 5)
      a = jnp.arange(np.prod(ashape)).reshape(ashape)
      bshape = (2, 7)
      b = jnp.arange(np.prod(bshape)).reshape(bshape)
      c, d = fm(a, b)
      self.assertAllClose(c, a * 2)
      self.assertAllClose(d, b * 4)

  testBasicSPMD = use_spmd_lowering(testBasic)

  @ignore_xmap_warning()
  def testBasicCollective(self):
    local_devices = list(jax.local_devices())
    if len(local_devices) < 4:
      raise SkipTest("Test requires at least 4 local devices")
    def f(a, b):
      return lax.psum(a * 2, 'a'), b * 4
    devices = np.array(local_devices[:4]).reshape((2, 2))
    with mesh(devices, ('x', 'y')):
      fm = xmap(f,
                in_axes=[A({'a': 0, 'b': 1}), A({'c': 0})],
                out_axes=[A({'b': 0}), A({'c': 0})],
                schedule=[
                  ('a', 'x'),
                  ('b', 'y'),
                  ('c', 'x'),
                  ('a', 'vectorize'),
                  ('b', 'vectorize'),
                ])
      ashape = (16, 8, 5)
      a = jnp.arange(np.prod(ashape)).reshape(ashape)
      bshape = (2, 7)
      b = jnp.arange(np.prod(bshape)).reshape(bshape)
      c, d = fm(a, b)
      self.assertAllClose(c, (a * 2).sum(0))
      self.assertAllClose(d, b * 4)

  testBasicCollectiveSPMD = use_spmd_lowering(testBasicCollective)

  @ignore_xmap_warning()
  @with_mesh([('x', 2)])
  def testCompilationCache(self):
    def f(x):
      assert python_should_be_executing
      return x * 2
    fm = xmap(f,
              in_axes=A({'a': 0}),
              out_axes=A({'a': 0}),
              schedule=[('a', 'x'), ('a', 'vectorize')])
    x = np.arange(8).reshape((2, 2, 2))
    python_should_be_executing = True
    fm(x)
    python_should_be_executing = False
    fm(x)

  @ignore_xmap_warning()
  @with_mesh([('x', 2)])
  def testNestedVectorize(self):
    @partial(xmap, in_axes=A({'a': 1}), out_axes=A({'a': 0}),
             schedule=[('a', 'x')])
    def f(x):
      y = x * 2
      @partial(xmap, in_axes=A({'b': 0}), out_axes=A({'b': 1}),
               schedule=[('b', 'vectorize')])
      def h(y):
        return jnp.sin(y)
      return h(y)
    xshape = (4, 2, 5)
    x = jnp.arange(np.prod(xshape)).reshape(xshape)
    self.assertAllClose(f(x),
                        jnp.sin(x * 2).transpose((1, 2, 0)))

  @ignore_xmap_warning()
  @with_mesh([('x', 2), ('y', 3)])
  def testNestedMesh(self):
    @partial(xmap, in_axes=A({'a': 1}), out_axes=A({'a': 0}),
             schedule=[('a', 'y')])
    def f(x):
      y = x * 2
      @partial(xmap, in_axes=A({'b': 0}), out_axes=A({'b': 1}),
               schedule=[('b', 'x')])
      def h(y):
        return jnp.sin(y)
      return h(y)
    xshape = (2, 3, 5)
    x = jnp.arange(np.prod(xshape)).reshape(xshape)
    y = f(x)
    self.assertAllClose(y, jnp.sin(x * 2).transpose((1, 2, 0)))
    # Make sure the op really ran accros a 2D mesh.
    self.assertEqual(y.sharding_spec.sharding,
                     (pxla.Chunked(3), None, None))
    self.assertEqual(y.sharding_spec.mesh_mapping,
                     (pxla.Replicated(2), pxla.ShardedAxis(0)))

  @ignore_xmap_warning()
  @with_mesh([('x', 2)])
  def testNestedDifferentResources(self):
    @partial(xmap, in_axes=A({'a': 0}), out_axes=A({'a': 0}),
             schedule=[('a', 'x')])
    def f(x):
      with mesh(np.empty((), dtype=np.object), ()):
        @partial(xmap, in_axes=A({'b': 0}), out_axes=A({'b': 0}),
                 schedule=[('b', 'vectorize')])
        def h(x):
          return x
        return h(x)
    xshape = (2, 5, 6)
    x = jnp.arange(np.prod(xshape)).reshape(xshape)
    with self.assertRaisesRegex(RuntimeError,
                                "Changing the resource environment.*"):
      f(x)

  @ignore_xmap_warning()
  @with_mesh([('r1', 2)])
  def testPdotBasic(self):
    def f(x, y):
      return lax.pdot(x, y, 'i')

    f_mapped = xmap(f, in_axes=[A({'i': 1}), A({'i': 0})], out_axes=A(),
                    schedule=[('i', 'r1'), ('i', 'vectorize')])

    rng = np.random.RandomState(0)
    x = rng.randn(3, 8)
    y = rng.randn(8, 5)

    z = f_mapped(x, y)

    self.assertAllClose(z, jnp.dot(x, y))

  @ignore_xmap_warning()
  @with_mesh([('r1', 2)])
  def testPdotBatching(self):
    def f(x, y):
      return lax.pdot(x, y, 'i')

    rng = np.random.RandomState(0)
    x = rng.randn(2, 3, 8)
    y = rng.randn(2, 8, 5)

    f_mapped = xmap(f,
                    in_axes=[A({'i': 2, 'j': 0}), A({'i': 1, 'j': 0})],
                    out_axes=A({'j': 0}),
                    schedule=[('j', 'vectorize'), ('i', 'r1'), ('i', 'vectorize')])

    z = f_mapped(x, y)

    self.assertAllClose(z, jnp.einsum('nij,njk->nik', x, y))

if __name__ == '__main__':
  absltest.main(testLoader=jtu.JaxTestLoader())
