import tensorflow as tf
import numpy as np
from glob import glob
import os
import re
from tensorflow.python import pywrap_tensorflow
import tqdm
import h5py
import shutil
import tempfile
import traceback

from tensorflow.contrib import tpu
from tensorflow.contrib.cluster_resolver import TPUClusterResolver
from tensorflow.python.framework import dtypes

import threading

def parallelize(xs, thunk, *args):
  threads = []
  for x in xs:
    thread = threading.Thread(target=thunk, args=(x, *args))
    thread.start()
    threads.append(thread)
  return threads

# http://stackoverflow.com/questions/1624883/alternative-way-to-split-a-list-into-groups-of-n
import itertools
def group(n, iterable, fillvalue=None):
    "group(3, 'ABCDEFG', 'x') --> ABC DEF Gxx"
    args = [iter(iterable)] * n
    return itertools.zip_longest(*args, fillvalue=fillvalue)

def tuples(*args, **kws):
  return [x for x in group(*args, **kws)]

class Namespace(object):
  pass

state = Namespace()

def get_tpu_addr(tpu_name=None):
    # Get the TPU's location
    if tpu_name is not None:
      return TPUClusterResolver(tpu_name).get_master()
    if 'COLAB_TPU_ADDR' in os.environ:
      return TPUClusterResolver().get_master()
    elif 'TPU_NAME' in os.environ:
      return TPUClusterResolver(os.environ['TPU_NAME']).get_master()

def get_session_target(target='auto'):
    if target == 'auto':
      target = get_tpu_addr()
    elif target is not None:
      target = get_tpu_addr(target)
    if target is not None:
      print("Using TPU %s" % target)
    return target

class Session(tf.Session):
  def __init__(self, target='auto', graph=None, config=None, init_tpu=False):
    target = get_session_target(target)
    super().__init__(target, graph=graph, config=config)
    self.init_tpu=init_tpu
    self.target = target
    self.config = config

  def ensure(self):
    if self.init_tpu:
      print("Initializing TPU...")
      #sess.run(tpu.initialize_system())
      initialize_tpu(session=self, timeout_in_ms=20000)
      self.init_tpu = None

def split_by_params(vs, n=20e6, f=None):
  if f is None:
    f = lambda x: np.prod(x.shape.as_list())
  i = 0
  xs = []
  for variable in vs:
    xs.append(variable)
    count = f(variable)
    i += count
    if i >= n:
      yield xs
      xs = []
      i = 0
  yield xs

def latest_checkpoint(checkpoint_dir, latest_filename=None):
  paths = [x for x in glob(os.path.join(checkpoint_dir, 'model-*.*')) if not x.endswith(".tmp")]
  ctrs = np.array([[int(y) for y in re.findall(r'model-([0-9]+)(?:-[0-9]+)?[.](?:npy|hdf5)', x)] for x in paths]).flatten()
  if len(ctrs) <= 0:
    ckpt = tf.train.latest_checkpoint(checkpoint_dir, latest_filename=latest_filename)
    return ckpt
  ctr = ctrs.max()
  return os.path.join(checkpoint_dir, 'model-{}').format(ctr)

def truncate_value(variable, value, reshape=True):
  if not reshape:
    return value
  shape = variable.shape.as_list()
  params = np.prod(shape)
  params2 = np.prod(value.shape)
  if params == params2:
    return value
  print('Truncating {} from shape {} to shape {}'.format(variable.name, value.shape, shape))
  value = np.array(value)
  value = value.reshape([-1])
  value = value[0:params]
  value = value.reshape(shape)
  return value

from tensorflow.core.protobuf import config_pb2

def initialize_tpu(session=None, timeout_in_ms=None):
  session = session or tf.get_default_session()
  with session.as_default():
    op = tpu.initialize_system()
  options = None
  if timeout_in_ms:
    options=config_pb2.RunOptions(timeout_in_ms=timeout_in_ms)
  return session.run(op, options=options)

def load(variable, value, session=None, timeout_in_ms=None):
  session = session or tf.get_default_session()
  ops = variable.initializer
  vals = dict([(variable.initializer.inputs[1], value)])
  #for x, (k, v) in zip(variables, vals.items()):
  #  print(x.name, x.shape.as_list(), k, v.shape)
  options = None
  if timeout_in_ms:
    options=config_pb2.RunOptions(timeout_in_ms=timeout_in_ms)
  return session.run(ops, vals, options=options)

def eval(variable, session=None, timeout_in_ms=None):
  session = session or tf.get_default_session()
  options = None
  if timeout_in_ms:
    options=config_pb2.RunOptions(timeout_in_ms=timeout_in_ms)
  return session.run(variable, options=options)

def grab_values(variables, reader, reshape=False):
  for variable in variables:
    name = variable_name(variable).split(':')[0]
    value = reader.get_tensor(name)
    value = truncate_value(variable, value, reshape=reshape)
    yield variable, value

def assign_values(variables, values, session=None, timeout_in_ms=60000):
  session = session or tf.get_default_session()
  ops = [x.initializer for x in variables]
  vals = dict([(x.initializer.inputs[1], value) for x, value in zip(variables, values)]) # TODO: bfloat16 support
  #for x, (k, v) in zip(variables, vals.items()):
  #  print(x.name, x.shape.as_list(), k, v.shape)
  options = None
  if timeout_in_ms:
    options=config_pb2.RunOptions(timeout_in_ms=timeout_in_ms)
  session.run(ops, vals, options=options)

def load_snapshot(ckpt, session=None, var_list=None, reshape=False):
  session = session or tf.get_default_session()
  reader = pywrap_tensorflow.NewCheckpointReader(ckpt)
  vs = var_list or tf.trainable_variables()
  for variables in tqdm.tqdm(list(split_by_params(vs))):
    values = [value for variable, value in grab_values(variables, reader, reshape=reshape)]
    assign_values(variables, values, session=session)

def get_variable(name, var_list=None):
  name, num = name.split(':') if ':' in name else (name, '0')
  num = int(num)
  name = os.path.join(tf.get_variable_scope().name, name)
  vs = var_list or tf.trainable_variables()
  for x in vs:
      if x.name.startswith(name + ':%d' % num):
          return x

def load_weights(ckpt, session=None, var_list=None, reshape=False):
  session = session or tf.get_default_session()
  vs = var_list or tf.trainable_variables()
  files = list(sorted(glob(ckpt + '-*.npy')))
  for out in tqdm.tqdm(files):
    for name, value in np.load(out, allow_pickle=True):
      variable = get_variable(name)
      if variable is None:
        print('Warning: variable %s not loaded' % name)
      else:
        value = truncate_value(variable, value, reshape=reshape)
        variable.load(value, session)

def load_variables(ckpt, session=None, var_list=None, reshape=False):
  session = session or tf.get_default_session()
  vs = var_list or tf.trainable_variables()
  with h5py.File(ckpt, "r") as f:
    for variables in tqdm.tqdm(list(split_by_params(vs))):
      values = [truncate_value(x, f[variable_name(x)], reshape=reshape)  for x in variables]
      assign_values(variables, values, session=session)

def maketree(path):
    try:
        os.makedirs(path)
    except:
        pass

state.cache_ops = {}

def cast_variables(variables, graph=None, cache_ops=None):
  if graph is None:
    graph = tf.get_default_graph()
  if cache_ops is None:
    cache_ops = state.cache_ops
  if graph not in cache_ops:
    cache_ops[graph] = {}
  cache = cache_ops[graph]
  ops = []
  for variable in variables:
    if variable in cache:
      op = cache[variable]
    elif variable.dtype == dtypes.bfloat16_ref or variable.dtype == tf.bfloat16:
      op = tf.cast(variable, tf.float32)
    else:
      op = variable
    cache[variable] = op
    ops.append(op)
  return ops

import re

def variable_name(variable):
  if re.match(r'core[0-9]+/', variable.name):
    return variable.name.split('/', 1)[-1]
  return variable.name

def save_variables(ckpt, session=None, var_list=None):
    session = session or tf.get_default_session()
    vs = var_list or tf.trainable_variables()
    maketree(os.path.dirname(ckpt))
    fname = ckpt+'.tmp'
    with h5py.File(fname, "w") as f:
      for variables in tqdm.tqdm(list(split_by_params(vs))):
        ops = cast_variables(variables)
        values = session.run(ops)
        for value, variable in zip(values, variables):
          name = variable_name(variable)
          shape = variable.shape.as_list()
          dtype = variable.dtype
          dset = f.create_dataset(name, shape, dtype=np.float32)
          dset[:] = value
    print('Writing snapshot %s' % ckpt)
    os.rename(ckpt+'.tmp', ckpt)

def fetch_variables(session=None, var_list=None):
    session = session or tf.get_default_session()
    vs = var_list or tf.trainable_variables()
    for variables in tqdm.tqdm(list(split_by_params(vs))):
      values = session.run(variables)
      yield variables, values

def partition_variables(session=None, var_list=None):
    session = session or tf.get_default_session()
    vs = var_list or tf.trainable_variables()
    for variables in tqdm.tqdm(list(split_by_params(vs))):
      yield variables

class Saver(object):
  def __init__(
    self,
    var_list=None,
    reshape=False,
    sharded=False,
    max_to_keep=5,
    keep_checkpoint_every_n_hours=10000.0,
    name=None,
    restore_sequentially=False,
    saver_def=None,
    builder=None,
    defer_build=False,
    allow_empty=False,
    write_version=tf.train.SaverDef.V2,
    pad_step_number=False,
    save_relative_paths=False,
    filename=None):
    self.var_list = var_list
    self.reshape = reshape
    self.sharded = sharded
    self.max_to_keep = max_to_keep
    self.keep_checkpoint_every_n_hours = keep_checkpoint_every_n_hours
    self.name = name
    self.restore_sequentially = restore_sequentially
    self.saver_def = saver_def
    self.builder = builder
    self.defer_build = defer_build
    self.allow_empty = allow_empty
    self.write_version = write_version
    self.pad_step_number = pad_step_number
    self.save_relative_paths = save_relative_paths
    self.filename = filename
    self.checkpoints = []

  def restore(self, sess, save_path):
    if save_path.endswith('.ckpt'):
      load_snapshot(save_path, session=sess, var_list=self.var_list, reshape=self.reshape)
    elif save_path.endswith('.hdf5'):
      load_variables(save_path, session=sess, var_list=self.var_list, reshape=self.reshape)
    elif os.path.exists(save_path + '.npy') or os.path.exists(save_path + '-0.npy'):
      load_weights(save_path, session=sess, var_list=self.var_list, reshape=self.reshape)
    elif os.path.exists(save_path + '.hdf5'):
      load_variables(save_path + '.hdf5', session=sess, var_list=self.var_list, reshape=self.reshape)
    else:
      raise Exception("Can't load checkpoint %s" % save_path)

  def save(self,
        sess,
        save_path,
        global_step=None,
        latest_filename=None,
        meta_graph_suffix="meta",
        write_meta_graph=True,
        write_state=True,
        strip_default_attrs=False,
        save_debug_info=False):
    if global_step is not None:
      name = '%s-%d.hdf5' % (save_path, global_step)
    else:
      name = '%s.hdf5' % save_path
    save_variables(name, session=sess, var_list=self.var_list)
    self.checkpoints.append(name)
    if self.max_to_keep > 0:
      while len(self.checkpoints) > self.max_to_keep:
        fname = self.checkpoints[0]
        if fname != name:
          print('Truncating %s' % fname)
          try:
            with open(fname, "wb") as f:
              pass
          except:
            print('Failed to truncate %s' % fname)
        self.checkpoints = self.checkpoints[1:]

  def fetch(self, sess, var_list=None):
    if var_list == None:
      var_list = self.var_list
    for variables, values in fetch_variables(session=sess, var_list=var_list):
      yield variables, values

  def variables(self, sess, var_list=None):
    if var_list == None:
      var_list = self.var_list
    for variables in partition_variables(session=sess, var_list=var_list):
      yield variables

  def assign(self, sess, variables, values):
    return assign_values(variables, values, session=sess)

class Commands(object):
  def __init__(self, path='commands'):
    self.path = path
    self.commands = []
    self.args = []
    self.keys = {}
    self.frozen = False

  def has(self, name, **keys):
    if 'action' in keys:
      action = keys.pop('action')
      for name1, action1 in self.commands:
        if name == name1 and action1 == action:
          return True
    else:
      for name1, action1 in self.commands:
        if name == name1:
          return True
    return False

  def add(self, name, action=None):
    if not self.has(name=name, action=action):
      self.commands.append((name, action))
      full = self.full_path(name)
      maketree(full)

  def full_path(self, name):
    return os.path.join(self.path, name)

  def check(self, *args, **keys):
    if not self.frozen:
      heartbeat()
    ops = []
    seen = set()
    for name, action in self.commands:
      full = self.full_path(name)
      if not os.path.isdir(full):
        if name not in seen:
          seen.add(name)
          ops.append(name)
    for op in ops:
      self.run(op, *args, **keys)
    return ops

  def run(self, op):
    ran = False
    for name, action in self.commands:
      if name == op:
        print('Running command', name, action)
        if not ran:
          full = self.full_path(op)
          maketree(full)
          ran = True
        if action:
          action()
    if not ran:
      raise Exception('Commands.execute failed: no such command: {}'.format(op))
  
  def run_with_args(self, op, *args, **keys):
    with CommandArgs(*args, **keys):
      return self.run(op)

commander = None

def commands(**keys):
  global commander
  if commander is None:
    commander = Commands()
  cmds = keys.pop('commands') if 'commands' in keys else None
  if cmds is not None:
    for cmd in cmds:
      action = None
      if isinstance(cmd, str):
        name = cmd
      elif len(cmd) >= 2:
        name, action = cmd
      elif len(cmd) >= 1:
        name = cmd[0]
      else:
        continue
      commander.add(name=name, action=action)
  return commander

class CommandArgs(object):
  def __init__(self, *args, **keys):
    self.args = list(args)
    self.keys = keys.copy()
    self.cmdr = commands()

  def __enter__(self):
    self.args_prev = self.cmdr.args
    self.keys_prev = self.cmdr.keys
    self.cmdr.args = self.args
    self.cmdr.keys = self.keys

  def __exit__(self, *excinfo):
    self.cmdr.args = self.args_prev
    self.cmdr.keys = self.keys_prev

def check_commands():
  try:
    cmdr = commands()
    return cmdr.check()
  except:
    traceback.print_exc()

def check_commands_with_args(*args, **keys):
  try:
    cmdr = commands()
    with CommandArgs(*args, **keys):
      return cmdr.check()
  except:
    traceback.print_exc()

def add_command(name, action=None, **keys):
  cmdr = commands()
  return cmdr.add(name=name, action=action)

def register_command(*args, **keys):
  fn = args[0]
  if isinstance(fn, str):
    add_command(fn)
  else:
    name = fn.__qualname__
    name = name.replace('.<locals>.', '_command_')
    if name.endswith('_command_save'):
      name = 'save'
    name = name.replace('___', '/')
    action = fn
    print(name, action)
    add_command(name, action)
  return fn

def has_command(name):
  cmdr = commands()
  return cmdr.has(name)

def run_command(command_name):
  cmdr = commands()
  return cmdr.run(command_name)

def run_command_with_args(command_name, *args, **keys):
  cmdr = commands()
  return cmdr.run_with_args(command_name, *args, **keys)

def command_arg(x, unset=None):
  cmdr = commands()
  if isinstance(x, int):
    try:
      return cmdr.args[x]
    except:
      return unset
  else:
    if x in cmdr.keys:
      return cmdr.keys[x]
    return unset

def command_args():
  cmdr = commands()
  return cmdr.args, cmdr.keys

@register_command
def attach_debugger():
  import pdb
  pdb.set_trace()

from pprint import pprint

@register_command
def print_status():
  args, props = command_args()
  for k, v in enumerate(args):
    pprint(v)
  for k, v in props.items():
    pprint({k: v})


#
# return current UTC timestamp.
#
def utc():
    from datetime import datetime
    d = datetime.utcnow()
    import calendar
    return calendar.timegm(d.utctimetuple())

def heartbeat():
  pongfile=os.environ['PONG'] if 'PONG' in os.environ else 'pong.txt'
  with open(pongfile, "a+") as f:
    nonce = os.urandom(8).hex()
    now=utc()
    out="pid{}_time{}_nonce{}\n".format(os.getpid(), now, nonce)
    #print("PONG! Writing {} to {}".format(out, pongfile))
    f.write(out)
    f.flush()

import time

@register_command
def freeze_forever():
  cmdr = commands()
  if cmdr.frozen:
    print("Already frozen.")
    return
  prev = cmdr.frozen
  cmdr.frozen = True
  print('Simulating a freeze; going into an infinite loop:')
  prev=time.time()
  try:
    while not should_quit():
      elapsed=time.time() - prev
      print('Frozen for {}s'.format(elapsed))
      time.sleep(1)
      check_commands()
  finally:
    cmdr.frozen = prev

_quit = False

import sys

@register_command
def quit():
  global _quit
  if _quit:
    print("Failed to quit; running sys.exit(1)")
    sys.exit(1)
  else:
    print("Quitting...")
    _quit = True

def should_quit():
  return _quit

@register_command
def save_and_quit():
  global _quit
  if has_command('save'):
    print("Saving...")
    run_command('save')
  quit()

@register_command
def throw_exception():
  raise Exception("This exception should be caught and logged by the tflex command system")


import tensorflow as tf
from contextlib import contextmanager

@contextmanager
def nullcontext(enter_result=None):
    yield enter_result

_devices = None
_has_gpu = False

def has_gpu():
  global _devices
  global _has_gpu
  if _devices is None:
    _devices = tf.get_default_session().list_devices()
    _has_gpu = len([x.name for x in _devices if ':GPU' in x.name]) > 0
  return _has_gpu

def device(name=''):
  if name is None:
    return tf.device(None)
  if 'gpu' in name:
    if has_gpu():
      return tf.device(name)
  if 'cpu' in name:
    return tf.device(name)
  return nullcontext()

