from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import namedtuple, OrderedDict
import multiprocessing
import os
import psutil
import random
import redis
import signal
import socket
import string
import subprocess
import sys
import time
import threading

# Ray modules
import ray.local_scheduler
import ray.plasma
import ray.global_scheduler as global_scheduler

PROCESS_TYPE_MONITOR = "monitor"
PROCESS_TYPE_WORKER = "worker"
PROCESS_TYPE_LOCAL_SCHEDULER = "local_scheduler"
PROCESS_TYPE_PLASMA_MANAGER = "plasma_manager"
PROCESS_TYPE_PLASMA_STORE = "plasma_store"
PROCESS_TYPE_GLOBAL_SCHEDULER = "global_scheduler"
PROCESS_TYPE_REDIS_SERVER = "redis_server"
PROCESS_TYPE_WEB_UI = "web_ui"

# This is a dictionary tracking all of the processes of different types that
# have been started by this services module. Note that the order of the keys is
# important because it determines the order in which these processes will be
# terminated when Ray exits, and certain orders will cause errors to be logged
# to the screen.
all_processes = OrderedDict([(PROCESS_TYPE_MONITOR, []),
                             (PROCESS_TYPE_WORKER, []),
                             (PROCESS_TYPE_LOCAL_SCHEDULER, []),
                             (PROCESS_TYPE_PLASMA_MANAGER, []),
                             (PROCESS_TYPE_PLASMA_STORE, []),
                             (PROCESS_TYPE_GLOBAL_SCHEDULER, []),
                             (PROCESS_TYPE_REDIS_SERVER, []),
                             (PROCESS_TYPE_WEB_UI, [])],)

# True if processes are run in the valgrind profiler.
RUN_LOCAL_SCHEDULER_PROFILER = False
RUN_PLASMA_MANAGER_PROFILER = False
RUN_PLASMA_STORE_PROFILER = False

# ObjectStoreAddress tuples contain all information necessary to connect to an
# object store. The fields are:
# - name: The socket name for the object store
# - manager_name: The socket name for the object store manager
# - manager_port: The Internet port that the object store manager listens on
ObjectStoreAddress = namedtuple("ObjectStoreAddress", ["name",
                                                       "manager_name",
                                                       "manager_port"])

def address(ip_address, port):
  return ip_address + ":" + str(port)

def get_ip_address(address):
  try:
    ip_address = address.split(":")[0]
  except:
    raise Exception("Unable to parse IP address from address {}".format(address))
  return ip_address

def get_port(address):
  try:
    port = int(address.split(":")[1])
  except:
    raise Exception("Unable to parse port from address {}".format(address))
  return port

def new_port():
  return random.randint(10000, 65535)

def random_name():
  return str(random.randint(0, 99999999))

def kill_process(p):
  """Kill a process.

  Args:
    p: The process to kill.

  Returns:
    True if the process was killed successfully and false otherwise.
  """
  if p.poll() is not None: # process has already terminated
    return True
  if RUN_LOCAL_SCHEDULER_PROFILER or RUN_PLASMA_MANAGER_PROFILER or RUN_PLASMA_STORE_PROFILER:
    os.kill(p.pid, signal.SIGINT) # Give process signal to write profiler data.
    time.sleep(0.1) # Wait for profiling data to be written.

  # Allow the process one second to exit gracefully.
  p.terminate()
  timer = threading.Timer(1, lambda p: p.kill(), [p])
  try:
    timer.start()
    p.wait()
  finally:
    timer.cancel()

  if p.poll() is not None:
    return True

  # If the process did not exit within one second, force kill it.
  p.kill()
  if p.poll() is not None:
    return True

  # The process was not killed for some reason.
  return False

def cleanup():
  """When running in local mode, shutdown the Ray processes.

  This method is used to shutdown processes that were started with
  services.start_ray_head(). It kills all scheduler, object store, and worker
  processes that were started by this services module. Driver processes are
  started and disconnected by worker.py.
  """
  successfully_shut_down = True
  # Terminate the processes in reverse order.
  for process_type in all_processes.keys():
    # Kill all of the processes of a certain type.
    for p in all_processes[process_type]:
      success = kill_process(p)
      successfully_shut_down = successfully_shut_down and success
    # Reset the list of processes of this type.
    all_processes[process_type] = []
  if not successfully_shut_down:
    print("Ray did not shut down properly.")

def all_processes_alive(exclude=[]):
  """Check if all of the processes are still alive.

  Args:
    exclude: Don't check the processes whose types are in this list.
  """
  for process_type, processes in all_processes.items():
    # Note that p.poll() returns the exit code that the process exited with, so
    # an exit code of None indicates that the process is still alive.
    if not all([p.poll() is None for p in processes]) and process_type not in exclude:
      return False
  return True

def get_node_ip_address(address="8.8.8.8:53"):
  """Determine the IP address of the local node.

  Args:
    address (str): The IP address and port of any known live service on the
      network you care about.

  Returns:
    The IP address of the current node.
  """
  ip_address, port = address.split(":")
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  s.connect((ip_address, int(port)))
  return s.getsockname()[0]

def wait_for_redis_to_start(redis_ip_address, redis_port, num_retries=5):
  """Wait for a Redis server to be available.

  This is accomplished by creating a Redis client and sending a random command
  to the server until the command gets through.

  Args:
    redis_ip_address (str): The IP address of the redis server.
    redis_port (int): The port of the redis server.
    num_retries (int): The number of times to try connecting with redis. The
      client will sleep for one second between attempts.

  Raises:
    Exception: An exception is raised if we could not connect with Redis.
  """
  redis_client = redis.StrictRedis(host=redis_ip_address, port=redis_port)
  # Wait for the Redis server to start.
  counter = 0
  while counter < num_retries:
    try:
      # Run some random command and see if it worked.
      print("Waiting for redis server at {}:{} to respond...".format(redis_ip_address, redis_port))
      redis_client.client_list()
    except redis.ConnectionError as e:
      # Wait a little bit.
      time.sleep(1)
      print("Failed to connect to the redis server, retrying.")
      counter += 1
    else:
      break
  if counter == num_retries:
    raise Exception("Unable to connect to Redis. If the Redis instance is on a different machine, check that your firewall is configured properly.")

def start_redis(port=None, num_retries=20, stdout_file=None, stderr_file=None,
                cleanup=True):
  """Start a Redis server.

  Args:
    port (int): If provided, start a Redis server with this port.
    num_retries (int): The number of times to attempt to start Redis.
    stdout_file: A file handle opened for writing to redirect stdout to. If no
      redirection should happen, then this should be None.
    stderr_file: A file handle opened for writing to redirect stderr to. If no
      redirection should happen, then this should be None.
    cleanup (bool): True if using Ray in local mode. If cleanup is true, then
      this process will be killed by serices.cleanup() when the Python process
      that imported services exits.

  Returns:
    A tuple of the port used by Redis and a handle to the process that was
      started. If a port is passed in, then the returned port value is the same.

  Raises:
    Exception: An exception is raised if Redis could not be started.
  """
  redis_filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "./core/src/common/thirdparty/redis/src/redis-server")
  redis_module = os.path.join(os.path.dirname(os.path.abspath(__file__)), "./core/src/common/redis_module/libray_redis_module.so")
  assert os.path.isfile(redis_filepath)
  assert os.path.isfile(redis_module)
  counter = 0
  if port is not None:
    if num_retries != 1:
      raise Exception("num_retries must be 1 if port is specified.")
  else:
    port = new_port()
  while counter < num_retries:
    if counter > 0:
      print("Redis failed to start, retrying now.")
    p = subprocess.Popen([redis_filepath,
                          "--port", str(port),
                          "--loglevel", "warning",
                          "--loadmodule", redis_module],
                          stdout=stdout_file, stderr=stderr_file)
    time.sleep(0.1)
    # Check if Redis successfully started (or at least if it the executable did
    # not exit within 0.1 seconds).
    if p.poll() is None:
      if cleanup:
        all_processes[PROCESS_TYPE_REDIS_SERVER].append(p)
      break
    port = new_port()
    counter += 1
  if counter == num_retries:
    raise Exception("Couldn't start Redis.")

  # Create a Redis client just for configuring Redis.
  redis_client = redis.StrictRedis(host="127.0.0.1", port=port)
  # Wait for the Redis server to start.
  wait_for_redis_to_start("127.0.0.1", port)
  # Configure Redis to generate keyspace notifications. TODO(rkn): Change this
  # to only generate notifications for the export keys.
  redis_client.config_set("notify-keyspace-events", "Kl")
  # Configure Redis to not run in protected mode so that processes on other
  # hosts can connect to it. TODO(rkn): Do this in a more secure way.
  redis_client.config_set("protected-mode", "no")
  # Put a time stamp in Redis to indicate when it was started.
  redis_client.set("redis_start_time", time.time())
  return port, p

def start_global_scheduler(redis_address, stdout_file=None, stderr_file=None,
                           cleanup=True):
  """Start a global scheduler process.

  Args:
    redis_address (str): The address of the Redis instance.
    stdout_file: A file handle opened for writing to redirect stdout to. If no
      redirection should happen, then this should be None.
    stderr_file: A file handle opened for writing to redirect stderr to. If no
      redirection should happen, then this should be None.
    cleanup (bool): True if using Ray in local mode. If cleanup is true, then
      this process will be killed by services.cleanup() when the Python process
      that imported services exits.
  """
  p = global_scheduler.start_global_scheduler(redis_address,
                                              stdout_file=stdout_file,
                                              stderr_file=stderr_file)
  if cleanup:
    all_processes[PROCESS_TYPE_GLOBAL_SCHEDULER].append(p)

def start_webui(redis_address, backend_stdout_file=None,
                backend_stderr_file=None, polymer_stdout_file=None,
                polymer_stderr_file=None, cleanup=True):
  """Attempt to start the Ray web UI.

  Args:
    redis_address (str): The address of the Redis server.
    backend_stdout_file: A file handle opened for writing to redirect the
      backend stdout to. If no redirection should happen, then this should be
      None.
    backend_stderr_file: A file handle opened for writing to redirect the
      backend stderr to. If no redirection should happen, then this should be
      None.
    polymer_stdout_file: A file handle opened for writing to redirect the
      polymer stdout to. If no redirection should happen, then this should be
      None.
    polymer_stderr_file: A file handle opened for writing to redirect the
      polymer stderr to. If no redirection should happen, then this should be
      None.
    cleanup (bool): True if using Ray in local mode. If cleanup is True, then
      this process will be killed by services.cleanup() when the Python process
      that imported services exits.

  Return:
    True if the web UI was successfully started, otherwise false.
  """
  webui_backend_filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../webui/backend/ray_ui.py")
  webui_directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../webui/")

  if sys.version_info >= (3, 0):
    python_executable = "python"
  else:
    # If the user is using Python 2, it is still possible to run the webserver
    # separately with Python 3, so try to find a Python 3 executable.
    try:
      python_executable = subprocess.check_output(["which", "python3"]).decode("ascii").strip()
    except Exception as e:
      print("Not starting the web UI because the web UI requires Python 3.")
      return False

  backend_process = subprocess.Popen([python_executable,
                                      webui_backend_filepath,
                                      "--redis-address", redis_address],
                                      stdout=backend_stdout_file,
                                      stderr=backend_stderr_file)

  time.sleep(0.1)
  if backend_process.poll() is not None:
    # Failed to start the web UI.
    print("The web UI failed to start.")
    return False

  # Try to start polymer. If this fails, it may that port 8080 is already in
  # use. It'd be nice to test for this, but doing so by calling "bind" may start
  # using the port and prevent polymer from using it.
  try:
    polymer_process = subprocess.Popen(["polymer", "serve", "--port", "8080"],
                                       cwd=webui_directory,
                                       stdout=polymer_stdout_file,
                                       stderr=polymer_stderr_file)
  except Exception as e:
    print("Failed to start polymer.")
    # Kill the backend since it won't work without polymer.
    try:
      backend_process.kill()
    except Exception as e:
      pass
    return False

  # Unfortunately this block of code is unlikely to catch any problems because
  # when polymer throws an error on startup, it is typically after several
  # seconds.
  time.sleep(0.1)
  if polymer_process.poll() is not None:
    # Failed to start polymer.
    print("Failed to serve the web UI with polymer.")
    # Kill the backend since it won't work without polymer.
    try:
      backend_process.kill()
    except Exception as e:
      pass
    return False

  if cleanup:
    all_processes[PROCESS_TYPE_WEB_UI].append(backend_process)
    all_processes[PROCESS_TYPE_WEB_UI].append(polymer_process)

  return True

def start_local_scheduler(redis_address,
                          node_ip_address,
                          plasma_store_name,
                          plasma_manager_name,
                          worker_path,
                          plasma_address=None,
                          stdout_file=None,
                          stderr_file=None,
                          cleanup=True,
                          num_cpus=None,
                          num_gpus=None,
                          num_workers=0):
  """Start a local scheduler process.

  Args:
    redis_address (str): The address of the Redis instance.
    node_ip_address (str): The IP address of the node that this local scheduler
      is running on.
    plasma_store_name (str): The name of the plasma store socket to connect to.
    plasma_manager_name (str): The name of the plasma manager socket to connect
      to.
    worker_path (str): The path of the script to use when the local scheduler
      starts up new workers.
    stdout_file: A file handle opened for writing to redirect stdout to. If no
      redirection should happen, then this should be None.
    stderr_file: A file handle opened for writing to redirect stderr to. If no
      redirection should happen, then this should be None.
    cleanup (bool): True if using Ray in local mode. If cleanup is true, then
      this process will be killed by serices.cleanup() when the Python process
      that imported services exits.
    num_cpus: The number of CPUs the local scheduler should be configured with.
    num_gpus: The number of GPUs the local scheduler should be configured with.
    num_workers (int): The number of workers that the local scheduler should
      start.

  Return:
    The name of the local scheduler socket.
  """
  if num_cpus is None:
    # By default, use the number of hardware execution threads for the number of
    # cores.
    num_cpus = multiprocessing.cpu_count()
  if num_gpus is None:
    # By default, assume this node has no GPUs.
    num_gpus = 0
  local_scheduler_name, p = ray.local_scheduler.start_local_scheduler(
      plasma_store_name,
      plasma_manager_name,
      worker_path=worker_path,
      node_ip_address=node_ip_address,
      redis_address=redis_address,
      plasma_address=plasma_address,
      use_profiler=RUN_LOCAL_SCHEDULER_PROFILER,
      stdout_file=stdout_file,
      stderr_file=stderr_file,
      static_resource_list=[num_cpus, num_gpus],
      num_workers=num_workers)
  if cleanup:
    all_processes[PROCESS_TYPE_LOCAL_SCHEDULER].append(p)
  return local_scheduler_name

def start_objstore(node_ip_address, redis_address, object_manager_port=None,
                   store_stdout_file=None, store_stderr_file=None,
                   manager_stdout_file=None, manager_stderr_file=None,
                   cleanup=True, objstore_memory=None):
  """This method starts an object store process.

  Args:
    node_ip_address (str): The IP address of the node running the object store.
    redis_address (str): The address of the Redis instance to connect to.
    object_manager_port (int): The port to use for the object manager. If this
      is not provided, one will be generated randomly.
    store_stdout_file: A file handle opened for writing to redirect stdout to.
      If no redirection should happen, then this should be None.
    store_stderr_file: A file handle opened for writing to redirect stderr to.
      If no redirection should happen, then this should be None.
    manager_stdout_file: A file handle opened for writing to redirect stdout to.
      If no redirection should happen, then this should be None.
    manager_stderr_file: A file handle opened for writing to redirect stderr to.
      If no redirection should happen, then this should be None.
    cleanup (bool): True if using Ray in local mode. If cleanup is true, then
      this process will be killed by serices.cleanup() when the Python process
      that imported services exits.
    objstore_memory: The amount of memory (in bytes) to start the object store
      with.

  Return:
    A tuple of the Plasma store socket name, the Plasma manager socket name, and
      the plasma manager port.
  """
  if objstore_memory is None:
    # Compute a fraction of the system memory for the Plasma store to use.
    system_memory = psutil.virtual_memory().total
    if sys.platform == "linux" or sys.platform == "linux2":
      # On linux we use /dev/shm, its size is half the size of the physical
      # memory. To not overflow it, we set the plasma memory limit to 0.4 times
      # the size of the physical memory.
      objstore_memory = int(system_memory * 0.4)
      # Compare the requested memory size to the memory available in /dev/shm.
      shm_fd = os.open("/dev/shm", os.O_RDONLY)
      try:
        shm_fs_stats = os.fstatvfs(shm_fd)
        # The value shm_fs_stats.f_bsize is the block size and the value
        # shm_fs_stats.f_bavail is the number of available blocks.
        shm_avail = shm_fs_stats.f_bsize * shm_fs_stats.f_bavail
        if objstore_memory > shm_avail:
          print("Warning: Reducing object store memory because /dev/shm has "
                "only {} bytes available. You may be able to free up space by "
                "deleting files in /dev/shm. If you are inside a Docker "
                "container, you may need to pass an argument with the flag "
                "'--shm-size' to 'docker run'.".format(shm_avail))
          objstore_memory = int(shm_avail * 0.8)
      finally:
        os.close(shm_fd)
    else:
      objstore_memory = int(system_memory * 0.8)
  # Start the Plasma store.
  plasma_store_name, p1 = ray.plasma.start_plasma_store(
      plasma_store_memory=objstore_memory,
      use_profiler=RUN_PLASMA_STORE_PROFILER,
      stdout_file=store_stdout_file,
      stderr_file=store_stderr_file)
  # Start the plasma manager.
  if object_manager_port is not None:
    plasma_manager_name, p2, plasma_manager_port = ray.plasma.start_plasma_manager(
        plasma_store_name,
        redis_address,
        plasma_manager_port=object_manager_port,
        node_ip_address=node_ip_address,
        num_retries=1,
        run_profiler=RUN_PLASMA_MANAGER_PROFILER,
        stdout_file=manager_stdout_file,
        stderr_file=manager_stderr_file)
    assert plasma_manager_port == object_manager_port
  else:
    plasma_manager_name, p2, plasma_manager_port = ray.plasma.start_plasma_manager(
        plasma_store_name,
        redis_address,
        node_ip_address=node_ip_address,
        run_profiler=RUN_PLASMA_MANAGER_PROFILER,
        stdout_file=manager_stdout_file,
        stderr_file=manager_stderr_file)
  if cleanup:
    all_processes[PROCESS_TYPE_PLASMA_STORE].append(p1)
    all_processes[PROCESS_TYPE_PLASMA_MANAGER].append(p2)

  return ObjectStoreAddress(plasma_store_name, plasma_manager_name,
                            plasma_manager_port)

def start_worker(node_ip_address, object_store_name, object_store_manager_name,
                 local_scheduler_name, redis_address, worker_path,
                 stdout_file=None, stderr_file=None, cleanup=True):
  """This method starts a worker process.

  Args:
    node_ip_address (str): The IP address of the node that this worker is
      running on.
    object_store_name (str): The name of the object store.
    object_store_manager_name (str): The name of the object store manager.
    local_scheduler_name (str): The name of the local scheduler.
    redis_address (str): The address that the Redis server is listening on.
    worker_path (str): The path of the source code which the worker process will
      run.
    stdout_file: A file handle opened for writing to redirect stdout to. If no
      redirection should happen, then this should be None.
    stderr_file: A file handle opened for writing to redirect stderr to. If no
      redirection should happen, then this should be None.
    cleanup (bool): True if using Ray in local mode. If cleanup is true, then
      this process will be killed by services.cleanup() when the Python process
      that imported services exits. This is True by default.
  """
  command = ["python",
             worker_path,
             "--node-ip-address=" + node_ip_address,
             "--object-store-name=" + object_store_name,
             "--object-store-manager-name=" + object_store_manager_name,
             "--local-scheduler-name=" + local_scheduler_name,
             "--redis-address=" + str(redis_address)]
  p = subprocess.Popen(command, stdout=stdout_file, stderr=stderr_file)
  if cleanup:
    all_processes[PROCESS_TYPE_WORKER].append(p)

def start_monitor(redis_address, stdout_file=None, stderr_file=None,
                  cleanup=True):
  """Run a process to monitor the other processes.

  Args:
    redis_address (str): The address that the Redis server is listening on.
    stdout_file: A file handle opened for writing to redirect stdout to. If no
      redirection should happen, then this should be None.
    stderr_file: A file handle opened for writing to redirect stderr to. If no
      redirection should happen, then this should be None.
    cleanup (bool): True if using Ray in local mode. If cleanup is true, then
      this process will be killed by services.cleanup() when the Python process
      that imported services exits. This is True by default.
  """
  monitor_path= os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.py")
  command = ["python",
             monitor_path,
             "--redis-address=" + str(redis_address)]
  p = subprocess.Popen(command, stdout=stdout_file, stderr=stderr_file)
  if cleanup:
    all_processes[PROCESS_TYPE_WORKER].append(p)

def start_ray_processes(address_info=None,
                        node_ip_address="127.0.0.1",
                        num_workers=0,
                        num_local_schedulers=1,
                        worker_path=None,
                        cleanup=True,
                        redirect_output=False,
                        include_global_scheduler=False,
                        include_redis=False,
                        include_webui=False,
                        start_workers_from_local_scheduler=True,
                        num_cpus=None,
                        num_gpus=None):
  """Helper method to start Ray processes.

  Args:
    address_info (dict): A dictionary with address information for processes
      that have already been started. If provided, address_info will be
      modified to include processes that are newly started.
    node_ip_address (str): The IP address of this node.
    num_workers (int): The number of workers to start.
    num_local_schedulers (int): The total number of local schedulers required.
      This is also the total number of object stores required. This method will
      start new instances of local schedulers and object stores until there are
      num_local_schedulers existing instances of each, including ones already
      registered with the given address_info.
    worker_path (str): The path of the source code that will be run by the
      worker.
    cleanup (bool): If cleanup is true, then the processes started here will be
      killed by services.cleanup() when the Python process that called this
      method exits.
    redirect_output (bool): True if stdout and stderr should be redirected to a
      file.
    include_global_scheduler (bool): If include_global_scheduler is True, then
      start a global scheduler process.
    include_redis (bool): If include_redis is True, then start a Redis server
      process.
    include_webui (bool): If True, then attempt to start the web UI. Note that
      this is only possible with Python 3.
    start_workers_from_local_scheduler (bool): If this flag is True, then start
      the initial workers from the local scheduler. Else, start them from
      Python.
    num_cpus: A list of length num_local_schedulers containing the number of
      CPUs each local scheduler should be configured with.
    num_gpus: A list of length num_local_schedulers containing the number of
      GPUs each local scheduler should be configured with.

  Returns:
    A dictionary of the address information for the processes that were
      started.
  """
  if not isinstance(num_cpus, list):
    num_cpus = num_local_schedulers * [num_cpus]
  if not isinstance(num_gpus, list):
    num_gpus = num_local_schedulers * [num_gpus]
  assert len(num_cpus) == num_local_schedulers
  assert len(num_gpus) == num_local_schedulers

  if address_info is None:
    address_info = {}
  address_info["node_ip_address"] = node_ip_address

  if worker_path is None:
    worker_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workers/default_worker.py")

  # Start Redis if there isn't already an instance running. TODO(rkn): We are
  # suppressing the output of Redis because on Linux it prints a bunch of
  # warning messages when it starts up. Instead of suppressing the output, we
  # should address the warnings.
  redis_address = address_info.get("redis_address")
  if include_redis:
    redis_stdout_file, redis_stderr_file = new_log_files("redis", redirect_output)
    if redis_address is None:
      # Start a Redis server. The start_redis method will choose a random port.
      redis_port, _ = start_redis(stdout_file=redis_stdout_file,
                                  stderr_file=redis_stderr_file,
                                  cleanup=cleanup)
      redis_address = address(node_ip_address, redis_port)
      address_info["redis_address"] = redis_address
      time.sleep(0.1)
    else:
      # A Redis address was provided, so start a Redis server with the given
      # port. TODO(rkn): We should check that the IP address corresponds to the
      # machine that this method is running on.
      redis_ip_address = get_ip_address(redis_address)
      redis_port = get_port(redis_address)
      new_redis_port, _ = start_redis(port=int(redis_port),
                                      num_retries=1,
                                      stdout_file=redis_stdout_file,
                                      stderr_file=redis_stderr_file,
                                      cleanup=cleanup)
      assert redis_port == new_redis_port
    # Start monitoring the processes.
    monitor_stdout_file, monitor_stderr_file = new_log_files("monitor", redirect_output)
    start_monitor(redis_address,
                  stdout_file=monitor_stdout_file,
                  stderr_file=monitor_stderr_file)
  else:
    if redis_address is None:
      raise Exception("Redis address expected")

  # Start the global scheduler, if necessary.
  if include_global_scheduler:
    global_scheduler_stdout_file, global_scheduler_stderr_file = new_log_files("global_scheduler", redirect_output)
    start_global_scheduler(redis_address,
                           stdout_file=global_scheduler_stdout_file,
                           stderr_file=global_scheduler_stderr_file,
                           cleanup=cleanup)

  # Initialize with existing services.
  if "object_store_addresses" not in address_info:
    address_info["object_store_addresses"] = []
  object_store_addresses = address_info["object_store_addresses"]
  if "local_scheduler_socket_names" not in address_info:
    address_info["local_scheduler_socket_names"] = []
  local_scheduler_socket_names = address_info["local_scheduler_socket_names"]

  # Get the ports to use for the object managers if any are provided.
  object_manager_ports = address_info["object_manager_ports"] if "object_manager_ports" in address_info else None
  if not isinstance(object_manager_ports, list):
    object_manager_ports = num_local_schedulers * [object_manager_ports]
  assert len(object_manager_ports) == num_local_schedulers

  # Start any object stores that do not yet exist.
  for i in range(num_local_schedulers - len(object_store_addresses)):
    # Start Plasma.
    plasma_store_stdout_file, plasma_store_stderr_file = new_log_files("plasma_store_{}".format(i), redirect_output)
    plasma_manager_stdout_file, plasma_manager_stderr_file = new_log_files("plasma_manager_{}".format(i), redirect_output)
    object_store_address = start_objstore(node_ip_address, redis_address,
                                          object_manager_port=object_manager_ports[i],
                                          store_stdout_file=plasma_store_stdout_file,
                                          store_stderr_file=plasma_store_stderr_file,
                                          manager_stdout_file=plasma_manager_stdout_file,
                                          manager_stderr_file=plasma_manager_stderr_file,
                                          cleanup=cleanup)
    object_store_addresses.append(object_store_address)
    time.sleep(0.1)

  # Determine how many workers to start for each local scheduler.
  num_workers_per_local_scheduler = [0] * num_local_schedulers
  for i in range(num_workers):
    num_workers_per_local_scheduler[i % num_local_schedulers] += 1

  # Start any local schedulers that do not yet exist.
  for i in range(len(local_scheduler_socket_names), num_local_schedulers):
    # Connect the local scheduler to the object store at the same index.
    object_store_address = object_store_addresses[i]
    plasma_address = "{}:{}".format(node_ip_address,
                                    object_store_address.manager_port)
    # Determine how many workers this local scheduler should start.
    if start_workers_from_local_scheduler:
      num_local_scheduler_workers = num_workers_per_local_scheduler[i]
      num_workers_per_local_scheduler[i] = 0
    else:
      # If we're starting the workers from Python, the local scheduler should
      # not start any workers.
      num_local_scheduler_workers = 0
    # Start the local scheduler.
    local_scheduler_stdout_file, local_scheduler_stderr_file = new_log_files("local_scheduler_{}".format(i), redirect_output)
    local_scheduler_name = start_local_scheduler(redis_address,
                                                 node_ip_address,
                                                 object_store_address.name,
                                                 object_store_address.manager_name,
                                                 worker_path,
                                                 plasma_address=plasma_address,
                                                 stdout_file=local_scheduler_stdout_file,
                                                 stderr_file=local_scheduler_stderr_file,
                                                 cleanup=cleanup,
                                                 num_cpus=num_cpus[i],
                                                 num_gpus=num_gpus[i],
                                                 num_workers=num_local_scheduler_workers)
    local_scheduler_socket_names.append(local_scheduler_name)
    time.sleep(0.1)

  # Make sure that we have exactly num_local_schedulers instances of object
  # stores and local schedulers.
  assert len(object_store_addresses) == num_local_schedulers
  assert len(local_scheduler_socket_names) == num_local_schedulers

  # Start any workers that the local scheduler has not already started.
  for i, num_local_scheduler_workers in enumerate(num_workers_per_local_scheduler):
    object_store_address = object_store_addresses[i]
    local_scheduler_name = local_scheduler_socket_names[i]
    for j in range(num_local_scheduler_workers):
      worker_stdout_file, worker_stderr_file = new_log_files("worker_{}_{}".format(i, j), redirect_output)
      start_worker(node_ip_address,
                   object_store_address.name,
                   object_store_address.manager_name,
                   local_scheduler_name,
                   redis_address,
                   worker_path,
                   stdout_file=worker_stdout_file,
                   stderr_file=worker_stderr_file,
                   cleanup=cleanup)
      num_workers_per_local_scheduler[i] -= 1

  # Make sure that we've started all the workers.
  assert(sum(num_workers_per_local_scheduler) == 0)

  # Try to start the web UI.
  if include_webui:
    backend_stdout_file, backend_stderr_file = new_log_files("webui_backend",
                                                             redirect_output=True)
    polymer_stdout_file, polymer_stderr_file = new_log_files("webui_polymer",
                                                             redirect_output=True)
    successfully_started = start_webui(redis_address,
                                       backend_stdout_file=backend_stdout_file,
                                       backend_stderr_file=backend_stderr_file,
                                       polymer_stdout_file=polymer_stdout_file,
                                       polymer_stderr_file=polymer_stderr_file,
                                       cleanup=cleanup)

    if successfully_started:
      print("View the web UI at http://localhost:8080.")

  # Return the addresses of the relevant processes.
  return address_info

def start_ray_node(node_ip_address,
                   redis_address,
                   object_manager_ports=None,
                   num_workers=0,
                   num_local_schedulers=1,
                   worker_path=None,
                   cleanup=True,
                   redirect_output=False,
                   num_cpus=None,
                   num_gpus=None):
  """Start the Ray processes for a single node.

  This assumes that the Ray processes on some master node have already been
  started.

  Args:
    node_ip_address (str): The IP address of this node.
    redis_address (str): The address of the Redis server.
    object_manager_ports (list): A list of the ports to use for the object
      managers. There should be one per object manager being started on this
      node (typically just one).
    num_workers (int): The number of workers to start.
    num_local_schedulers (int): The number of local schedulers to start. This is
      also the number of plasma stores and plasma managers to start.
    worker_path (str): The path of the source code that will be run by the
      worker.
    cleanup (bool): If cleanup is true, then the processes started here will be
      killed by services.cleanup() when the Python process that called this
      method exits.
    redirect_output (bool): True if stdout and stderr should be redirected to a
      file.

  Returns:
    A dictionary of the address information for the processes that were
      started.
  """
  address_info = {
      "redis_address": redis_address,
      "object_manager_ports": object_manager_ports,
      }
  return start_ray_processes(address_info=address_info,
                             node_ip_address=node_ip_address,
                             num_workers=num_workers,
                             num_local_schedulers=num_local_schedulers,
                             worker_path=worker_path,
                             cleanup=cleanup,
                             redirect_output=redirect_output,
                             num_cpus=num_cpus,
                             num_gpus=num_gpus)

def start_ray_head(address_info=None,
                   node_ip_address="127.0.0.1",
                   num_workers=0,
                   num_local_schedulers=1,
                   worker_path=None,
                   cleanup=True,
                   redirect_output=False,
                   start_workers_from_local_scheduler=True,
                   num_cpus=None,
                   num_gpus=None):
  """Start Ray in local mode.

  Args:
    address_info (dict): A dictionary with address information for processes
      that have already been started. If provided, address_info will be
      modified to include processes that are newly started.
    node_ip_address (str): The IP address of this node.
    num_workers (int): The number of workers to start.
    num_local_schedulers (int): The total number of local schedulers required.
      This is also the total number of object stores required. This method will
      start new instances of local schedulers and object stores until there are
      at least num_local_schedulers existing instances of each, including ones
      already registered with the given address_info.
    worker_path (str): The path of the source code that will be run by the
      worker.
    cleanup (bool): If cleanup is true, then the processes started here will be
      killed by services.cleanup() when the Python process that called this
      method exits.
    redirect_output (bool): True if stdout and stderr should be redirected to a
      file.
    start_workers_from_local_scheduler (bool): If this flag is True, then start
      the initial workers from the local scheduler. Else, start them from
      Python.
    num_cpus (int): number of cpus to configure the local scheduler with.
    num_gpus (int): number of gpus to configure the local scheduler with.

  Returns:
    A dictionary of the address information for the processes that were
      started.
  """
  return start_ray_processes(address_info=address_info,
                             node_ip_address=node_ip_address,
                             num_workers=num_workers,
                             num_local_schedulers=num_local_schedulers,
                             worker_path=worker_path,
                             cleanup=cleanup,
                             redirect_output=redirect_output,
                             include_global_scheduler=True,
                             include_redis=True,
                             include_webui=True,
                             start_workers_from_local_scheduler=start_workers_from_local_scheduler,
                             num_cpus=num_cpus,
                             num_gpus=num_gpus)

def new_log_files(name, redirect_output):
  """Generate partially randomized filenames for log files.

  Args:
    name (str): descriptive string for this log file.
    redirect_output (bool): True if files should be generated for logging stdout
      and stderr and false if stdout and stderr should not be redirected.

  Returns:
    If redirect_output is true, this will return a tuple of two filehandles. The
      first is for redirecting stdout and the second is for redirecting stderr.
      If redirect_output is false, this will return a tuple of two None objects.
  """
  if not redirect_output:
    return None, None

  logs_dir = "/tmp/raylogs"
  if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)
  log_id = random.randint(0, 100000)
  log_stdout = "{}/{}-{:06d}.out".format(logs_dir, name, log_id)
  log_stderr = "{}/{}-{:06d}.err".format(logs_dir, name, log_id)
  log_stdout_file = open(log_stdout, "a")
  log_stderr_file = open(log_stderr, "a")
  return log_stdout_file, log_stderr_file
