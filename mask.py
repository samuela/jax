from functools import partial
import itertools as it
import operator

import numpy as onp

from jax import core
from jax.core import Trace, Tracer
from jax.util import unzip2, prod, safe_map, safe_zip, split_list
from jax import linear_util as lu
from jax.abstract_arrays import ShapedArray
from jax.interpreters import partial_eval as pe
from jax import lax

map = safe_map
zip = safe_zip


Var = str

def mask(fun, shape_env, in_vals, shape_exprs):
  with core.new_master(MaskTrace) as master:
    fun, out_shapes = mask_subtrace(fun, master, shape_env)
    out_vals = fun.call_wrapped(in_vals, shape_exprs)
    del master
  return out_vals, out_shapes()

@lu.transformation_with_aux
def mask_subtrace(master, shape_env, in_vals, shape_exprs):
  trace = MaskTrace(master, core.cur_sublevel())
  in_tracers = map(partial(MaskTracer, trace, shape_env),
                   in_vals, shape_exprs)
  outs = yield in_tracers, {}
  out_tracers = map(trace.full_raise, outs)
  out_vals, out_shapes = unzip2((t.val, t.shape_expr) for t in out_tracers)
  yield out_vals, out_shapes

class ShapeExpr(object):
  def __init__(self, *shape):
    assert all(isinstance(s, (int, str)) for s in shape)
    self.shape = tuple(shape)
  def __iter__(self):
    return iter(self.shape)
  def __repr__(self):
    return 'ShapeExpr({})'.format(repr(self.shape))
  __str__ = __repr__
  def __eq__(self, other):
    return type(other) is ShapeExpr and self.shape == other.shape
Shape = ShapeExpr

class ShapeError(Exception): pass

class MaskTracer(Tracer):
  __slots__ = ["val", "shape_expr", "shape_env"]

  def __init__(self, trace, shape_env, val, shape_expr):
    self.trace = trace
    self.shape_env = shape_env
    self.val = val
    self.shape_expr = shape_expr

  @property
  def aval(self):
    # TODO can avoid some blowups, also improve error messages
    if self.shape_env is not None:
      shape = [self.shape_env[d] if type(d) is Var else d
              for d in self.shape_expr]
      return ShapedArray(tuple(shape), self.val.dtype)
    else:
      return ShapedArray(self.val.shape, self.val.dtype)

  def full_lower(self):
    if all(type(s) is int for s in self.shape_expr):
      return core.full_lower(self.val)
    else:
      return self

class MaskTrace(Trace):
  def pure(self, val):
    return MaskTracer(self, None, val, ShapeExpr(*onp.shape(val)))

  def lift(self, val):
    return MaskTracer(self, None, val, ShapeExpr(*onp.shape(val)))

  def sublift(self, val):
    return MaskTracer(self, val.shape_env, val.val, val.shape_expr)

  def process_primitive(self, primitive, tracers, params):
    shape_env = next(t.shape_env for t in tracers if t.shape_env is not None)
    vals, shape_exprs = unzip2((t.val, t.shape_expr) for t in tracers)
    rule = masking_rules[primitive]
    out, out_shape = rule(shape_env, vals, shape_exprs, **params)
    if not primitive.multiple_results:
      return MaskTracer(self, shape_env, out, out_shape)
    else:
      return map(partial(MaskTracer, self, shape_env), out, out_shape)

  def process_call(self, call_primitive, f, tracers, params):
    raise NotImplementedError  # TODO

masking_rules = {}


def reduce_sum_masking_rule(shape_env, vals, shape_exprs, axes, input_shape):
  val, = vals
  in_shape, = shape_exprs
  masks = [lax.broadcasted_iota(onp.int32, val.shape, i) < shape_env[d]
           for i, d in enumerate(in_shape) if type(d) is Var]
  mask = reduce(operator.and_, masks)
  masked_val = lax.select(mask, val, lax.zeros_like_array(val))
  out_val = lax.reduce_sum_p.bind(masked_val, axes=axes,
                                  input_shape=masked_val.shape)
  out_shape = ShapeExpr(*(d for i, d in enumerate(in_shape) if i not in axes))
  return out_val, out_shape
masking_rules[lax.reduce_sum_p] = reduce_sum_masking_rule

def add_masking_rule(shape_env, vals, shape_exprs):
  x, y = vals
  x_shape, y_shape = shape_exprs
  if x_shape == y_shape:
    return x + y, x_shape
  else:
    raise ShapeError
masking_rules[lax.add_p] = add_masking_rule

def scan_masking_rule(shape_env, vals, shape_exprs, forward, length, jaxpr,
                      num_consts, num_carry, linear):
  # TODO specialized for case where only leading extensive dimension is masked
  # TODO assert xs_shapes[0] after deref are all length
  dynamic_length = shape_env[length] if type(length) is Var else length
  consts, init, xs = split_list(vals, [num_consts, num_carry])
  max_length, = {x.shape[0] for x in xs}
  const_shapes, init_shapes, xs_shapes = split_list(shape_exprs, [num_consts, num_carry])
  _, y_avals = split_list(jaxpr.out_avals, [num_carry])
  out_shapes = _masked_scan_shape_rule(length, init_shapes, y_avals)
  masked_jaxpr = _masked_scan_jaxpr(jaxpr, dynamic_length, num_consts, num_carry)
  const_linear, init_linear, xs_linear = split_list(linear, [num_consts, num_carry])
  out_vals = lax.scan_p.bind(
      *it.chain(consts, [0], init, xs),
      forward=forward, length=max_length, jaxpr=masked_jaxpr,
      num_consts=num_consts, num_carry=1 + num_carry,
      linear=const_linear + [False] + init_linear + xs_linear)
  return out_vals[1:], out_shapes
masking_rules[lax.scan_p] = scan_masking_rule

def _masked_scan_shape_rule(length, carry_shapes, y_avals):
  ys_shapes = [ShapeExpr(length, *y_aval.shape) for y_aval in y_avals]
  return carry_shapes + ys_shapes

def _masked_scan_jaxpr(jaxpr, dynamic_length, num_consts, num_carry):
  fun = core.jaxpr_as_fun(jaxpr)

  @lu.wrap_init
  def masked(*args):
    consts, i_carry, xs = split_list(args, [num_consts, num_carry + 1])
    i, carry = i_carry[0], i_carry[1:]
    out = fun(*(carry + xs))
    new_carry, ys = split_list(out, [num_carry])
    new_carry = [lax.select(i < dynamic_length, new_c, c)
                 for new_c, c in zip(new_carry, carry)]
    return [i + 1] + new_carry + ys

  i_aval = ShapedArray((), onp.int32)
  const_avals, carry_avals, x_avals = split_list(jaxpr.in_avals, [num_consts, num_carry])
  return _make_typed_jaxpr(masked, const_avals + [i_aval] + carry_avals + x_avals)

def _make_typed_jaxpr(traceable, in_avals):
  pvals = [pe.PartialVal((aval, core.unit)) for aval in in_avals]
  jaxpr, pvals_out, consts = pe.trace_to_jaxpr(traceable, pvals, instantiate=True)
  out_avals, _ = unzip2(pvals_out)
  return core.TypedJaxpr(jaxpr, consts, in_avals, out_avals)


###

def pad(fun, in_shapes, out_shapes):
  def wrapped_fun(args, shape_env):
    # TODO check max sizes agree (i.e. padded / arg sizes agree) according to
    # shape vars
    outs, out_shapes_ = mask(lu.wrap_init(fun), shape_env, args, in_shapes)
    assert tuple(out_shapes_) == tuple(out_shapes)
    # TODO could check max size is what we expect according to input max sizes
    return outs
  return wrapped_fun

###

import jax.numpy as np
from jax import vmap

@partial(pad, in_shapes=[Shape('n')], out_shapes=[Shape()])
def padded_sum(x):
  return np.sum(x),  # output shape ()

print padded_sum([np.arange(5)], dict(n=3))
print vmap(padded_sum)([np.ones((5, 10))], dict(n=np.arange(5)))


@partial(pad, in_shapes=[Shape('n'), Shape('n')], out_shapes=[Shape('n')])
def addvecs(x, y):
  return x + y,

print addvecs([np.arange(5), np.arange(5)], dict(n=3))
# this is an error because padded sizes must agree
# print addvecs([np.arange(5), np.arange(6)], dict(n=3))


def cumsum_(arr):
  out, _ = lax.scan(lambda c, x: (c + x, ()), 0, arr)
  return out

@partial(pad, in_shapes=[Shape('n')], out_shapes=[Shape()])
def cumsum(x):
  return cumsum_(x),

print cumsum([np.array([5, 2, 9, 1, 4])], dict(n=3))
print vmap(cumsum)([np.arange(6).reshape(2, 3)], dict(n=np.array([1, 2])))


# notes!
# - a shape variable is associated with a max size and a dynamic size. we carry
#   around the dynamic size explicitly in the shape_env attached to every
#   tracer, while the max size we get off the val
# - we don't want to do the padding at the start and slicing at the end in the
#   transformation because we want to be able to vmap it, also want to jit it
#   - we could have a ragged array data type, or other api options
# - if we split up shape rules and evaluation rules, then the shape rules should
#   take the shapes before deref while the eval rules should take the dereffed
#   ones
# - we should probably pass the max size explicitly rather than getting it off
#   the values, e.g. the iota problem, should think of it as an independent type
#   argument
