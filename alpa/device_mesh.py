"""The device mesh runtime that manages buffers and runs computation distributedly."""
from collections import defaultdict, namedtuple
from collections.abc import Iterable
import cupy
from cupy.cuda import nccl
import logging
from operator import attrgetter
import time
from typing import Any, List, Union, Sequence, Tuple, Optional

import numpy as np
import ray

from jax import core, xla, eval_shape, device_put
from jax._src.api import ShapeDtypeStruct
from jax._src.util import unzip3
from jax.abstract_arrays import array_types
from jax.core import ShapedArray
from jax.interpreters import pxla
from jax.interpreters.pxla import (ShardingSpec, _as_slice_indices,
                                   _hashable_index, ShardedDeviceArray, Index)
from jax.lib import xla_client
import jax.numpy as jnp

from alpa import mesh_profiling
import alpa.collective as col
from alpa.collective.collective_group import nccl_util
from alpa.global_env import global_config
from alpa.mesh_executable import RemoteBufferRef, MeshDriverExecutable
from alpa.monkey_patch import set_override_backend
from alpa.shard_parallel.auto_sharding import LogicalDeviceMesh
from alpa.timer import timers
from alpa.util import (benchmark_func, list_gpu_info, jax_tensor_to_cupy,
                       cupy_to_jax_tensor, jax_tensor_set,
                       xla_buffer_to_jax_tensor, jax_tensor_to_xla_buffer,
                       xla_buffer_to_cupy, cupy_to_xla_buffer,
                       is_continuous_subset, infer_offset_and_n_elements,
                       jax_tensor_index, OrderedSet)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def device_id_to_str(host_ip, device_id, device_type="gpu"):
    """Convert device id (int) to a canonical device string."""
    return "{}:{}:{}".format(host_ip, device_type, str(device_id))


ReshardingTileSpec = namedtuple("ReshardingSendSpec",
                                ["offset", "rank", "gpu_idx"])
ReshardingSendTask = namedtuple("ReshardingSendTask",
                                ["tile_specs", "group_name"])
ReshardingRecvSpec = namedtuple("ReshardingRecvSpec",
                                ["device_id", "shape", "dtype", "tile_specs"])
ReshardingRecvTask = namedtuple("ReshardingRecvTask",
                                ["recv_specs", "group_name"])
ReshardingAllGatherSpec = namedtuple("ReshardingAllGatherSpec",
                                     ["device_ids", "tensor_slices"])
ReshardingAllGatherTask = namedtuple("ReshardingAllGatherTask",
                                     ["allgather_specs"])


class MeshHostWorker:
    """A ray actor that manages the xla computation and buffers on a single host."""

    def __init__(self, server_address: str, num_hosts: int, host_id: int):
        self.num_hosts = num_hosts
        self.host_id = host_id
        self.distributed_client = (
            xla_client._xla.get_distributed_runtime_client(
                server_address, host_id))
        logger.debug(
            f"{host_id}: Trying to connect to xla runtime at {server_address}")
        status = self.distributed_client.connect()
        logger.debug(
            f"{host_id}: Success to connect to xla runtime at {server_address}")
        self.backend = xla_client.make_gpu_client(self.distributed_client,
                                                  node_id=host_id)
        # Monkey patch the backend
        self.local_devices = self.backend.local_devices()
        self.allgather_communicators = {}
        self.buffers = {}  # Dict[uuid -> DeviceArray]
        self.executables = {}
        self.send_tasks = {}  # Dict[uuid -> ReshardingSendTask]
        self.recv_tasks = {}  # Dict[uuid -> ReshardingRecvTask]
        self.allgather_tasks = {}  # Dict[uuid -> AllgatherTask]
        set_override_backend(self.backend)

        if global_config.pipeline_use_signal_send_recv:
            print("Use signal send recv.")
            self.signal_tensors = []
            for d in self.local_devices:
                self.signal_tensors.append(
                    jax_tensor_to_cupy(device_put(
                        jnp.ones((1,), dtype=jnp.int8), d),
                                       take_ownership=True))

    ##### Buffer Related Functions #####
    def put_buffer(self, uuid: int, device_id: int, data: np.ndarray):
        assert uuid not in self.buffers
        self.buffers[uuid] = (self.backend.buffer_from_pyval(
            data, self.local_devices[device_id]))

    def put_non_zero_buffer(self,
                            uuid: int,
                            device_id: int,
                            shape: Sequence[int],
                            dtype=np.float32):
        self.buffers[uuid] = device_put(jnp.full(
            shape, 1e-8, dtype), self.local_devices[device_id]).device_buffer

    def get_buffers(self, uuids: Union[Sequence[int], int]):
        if isinstance(uuids, Iterable):
            return [self.buffers[uuid] for uuid in uuids]
        return self.buffers[uuids]

    def delete_buffers(self, uuids: Union[Sequence[int], int]):
        if isinstance(uuids, Iterable):
            for uuid in uuids:
                del self.buffers[uuid]
        else:
            del self.buffers[uuids]

    def block_until_ready_buffers(self, uuids: Union[Sequence[int], int]):
        if isinstance(uuids, Iterable):
            for uuid in uuids:
                self.buffers[uuid].block_until_ready()
        else:
            self.buffers[uuids].block_until_ready()

    def get_memory_allocated(self):
        self.sync()
        return max(d.memory_allocated() for d in self.local_devices)

    def get_max_memory_allocated(self):
        self.sync()
        return max(d.max_memory_allocated() for d in self.local_devices)

    def get_available_memory(self):
        self.sync()
        return min(d.available_memory() for d in self.local_devices)

    def reset_memory_stats(self):
        self.sync()
        for device in self.local_devices:
            device.clear_memory_stats()

    ##### Executable Related Functions #####
    def put_executable(self, uuid: int, executable_class, *args):
        self.executables[uuid] = executable_class(self, uuid, *args)

    def delete_executable(self, uuid: int):
        del self.executables[uuid]

    def run_executable(self, uuid: int, *args, **kwargs):
        self.executables[uuid].execute_on_worker(*args, **kwargs)

    def get_exec_total_allocation_size(self, uuid: int):
        return self.executables[uuid].get_total_allocation_size()

    ##### Cross Mesh Resharding Related Functions #####
    @staticmethod
    def init_collective_group(world_size, rank, backend, group_name):
        """Initialize the collective group eagerly."""
        col.init_collective_group(world_size,
                                  rank,
                                  backend=backend,
                                  group_name=group_name)
        return 0

    # Note: in this device mesh code, we will use 3 types of tensors:
    # (1) JAX high-level _DeviceArray, which is index-able, has __cuda_array__ interface
    # (2) XLA low-level PyLocalBuffer, which is not index-able
    # (3) cupy array, which is an intermediate format for ray collective
    def send_tile(self, uuid: int, offset: Sequence[slice], dst_rank: int,
                  dst_gpu_idx: int, group_name: str):
        """
        Send a slice of a source buffer to a target GPU.

        Args:
            uuid: the uuid of the xla buffers.
            offset: the slice to be sent in the buffer.
            dst_rank: destination rank to send.
            dst_gpu_idx: the gpu index on the destination rank.
            group_name: collective group name
        """
        if global_config.pipeline_use_signal_send_recv:
            signal = self.signal_tensors[uuid % len(self.local_devices)]
            col.send_multigpu(signal, dst_rank, dst_gpu_idx, group_name)
            return

        tensor_shape = self.buffers[uuid].shape
        if is_continuous_subset(offset, tensor_shape):
            # fast path, two cases: (1) same shape, (2) continuous subset.
            slice_shape = tuple(ind.stop - ind.start for ind in offset)
            to_send = xla_buffer_to_cupy(self.buffers[uuid])
            if slice_shape == tensor_shape:
                col.send_multigpu(to_send, dst_rank, dst_gpu_idx, group_name)
            else:
                ind, n_elements = infer_offset_and_n_elements(offset)
                col.send_multigpu(to_send[ind],
                                  dst_rank,
                                  dst_gpu_idx,
                                  group_name,
                                  n_elements=n_elements)
        else:
            # slower path, because of indexing.
            start_indices = tuple(o.start for o in offset)
            slice_sizes = tuple(o.stop - o.start for o in offset)
            src_buffer = jax_tensor_index(
                xla_buffer_to_jax_tensor(self.buffers[uuid]), start_indices,
                slice_sizes)
            to_send = jax_tensor_to_cupy(src_buffer)
            col.send_multigpu(to_send, dst_rank, dst_gpu_idx, group_name)

    def recv_tile(self, uuid: int, device_id: int,
                  indices_in_dst_tile: Sequence[slice], src_rank: int,
                  src_gpu_idx: int, group_name: str):
        """
        Receive a slice from a source GPU and in-place write it on the target buffer.

        Args:
            uuid: the uuid of the xla buffers.
            device_id: the device where the buffer is received, used to allocate tmp buffer.
            indices_in_dst_tile: the slice index to be written on destination buffer.
            src_rank: source rank to receive from.
            src_gpu_idx: the sender gpu index on the source rank.
            group_name: collective group name.
        """
        if uuid not in self.buffers:
            raise RuntimeError("Buffer has not been created.")

        if global_config.pipeline_use_signal_send_recv:
            signal = self.signal_tensors[uuid % len(self.local_devices)]
            col.recv_multigpu(signal, src_rank, src_gpu_idx, group_name)
            return

        tensor_shape = self.buffers[uuid].shape
        slice_shape = tuple(ind.stop - ind.start for ind in indices_in_dst_tile)
        if is_continuous_subset(indices_in_dst_tile, tensor_shape):
            to_recv = xla_buffer_to_cupy(self.buffers[uuid],
                                         take_ownership=True)
            if slice_shape == tensor_shape:
                col.recv_multigpu(to_recv, src_rank, src_gpu_idx, group_name)
            else:
                ind, n_elements = infer_offset_and_n_elements(
                    indices_in_dst_tile)
                col.recv_multigpu(to_recv[ind],
                                  src_rank,
                                  src_gpu_idx,
                                  group_name,
                                  n_elements=n_elements)
            self.buffers[uuid] = cupy_to_xla_buffer(to_recv)
        else:
            # The following call will allocate memory and cause a few H2D and D2D kernels.
            # See:https://github.com/alpa-projects/alpa/issues/145
            tmp_buffer = device_put(
                jnp.ones(slice_shape, dtype=self.buffers[uuid].dtype),
                self.local_devices[device_id])
            to_recv = jax_tensor_to_cupy(tmp_buffer, take_ownership=True)
            col.recv_multigpu(to_recv, src_rank, src_gpu_idx, group_name)
            recv_tensor = cupy_to_jax_tensor(to_recv)
            start_indices = tuple(
                ind_in_dst.start for ind_in_dst in indices_in_dst_tile)

            # The following in-place write will cause a D2D copy kernel
            # See: https://github.com/alpa-projects/alpa/issues/144
            # It is unavoidable, but it is better than:
            # new_buffer = dynamic_update_slice(src_buf, update, start_indices)
            # which is not in-place and will cause extra allocation-related kernels.
            new_buffer = jax_tensor_set(
                xla_buffer_to_jax_tensor(self.buffers[uuid]), recv_tensor,
                start_indices)
            self.buffers[uuid] = jax_tensor_to_xla_buffer(new_buffer)

    def init_p2p_communicator(self, group_name, my_rank, my_gpu_idx, peer_rank,
                              peer_gpu_idx, nccl_uid):
        """Initialize the P2P communicator from within the mesh workers."""
        assert col.is_group_initialized(group_name)
        assert col.get_rank(group_name) == my_rank
        g = col.check_and_get_group(group_name)
        g.create_p2p_communicator(my_gpu_idx, peer_rank, peer_gpu_idx, nccl_uid)
        return True

    def generate_nccl_uid(self, group_name):
        """Generate the NCCL unique ID in advance."""
        g = col.check_and_get_group(group_name)
        uid = g.generate_nccl_uid()
        return uid

    def allgather(self, uuids: Sequence[int], device_ids: Sequence[int],
                  tensor_slices: Sequence[slice]):
        cupy_buffers = []
        communicators = self.allgather_communicators[repr(sorted(device_ids))]
        relative_idx = dict(zip(sorted(device_ids), range(len(device_ids))))
        nccl_util.groupStart()
        for device_id, tensor_slice in zip(device_ids, tensor_slices):
            uuid = uuids[device_id]
            xla_buffer = self.buffers[uuid]
            cupy_buffer = xla_buffer_to_cupy(xla_buffer, take_ownership=True)
            ind, n_elements = infer_offset_and_n_elements(tensor_slice)
            cupy_slice = cupy_buffer[ind]
            communicators[relative_idx[device_id]].allGather(
                nccl_util.get_tensor_ptr(cupy_slice),
                nccl_util.get_tensor_ptr(cupy_buffer), n_elements,
                nccl_util.get_nccl_tensor_dtype(cupy_buffer),
                cupy.cuda.Stream.null.ptr)
            cupy_buffers.append(cupy_buffer)
        nccl_util.groupEnd()
        for device_id, cupy_buffer in zip(device_ids, cupy_buffers):
            uuid = uuids[device_id]
            self.buffers[uuid] = cupy_to_xla_buffer(cupy_buffer)

    def put_resharding_send_task(self, uuid, tasks, group_name):
        self.send_tasks[uuid] = ReshardingSendTask(tile_specs=tasks,
                                                   group_name=group_name)

    def put_resharding_recv_task(self, uuid, tasks, group_name):
        self.recv_tasks[uuid] = ReshardingRecvTask(recv_specs=tasks,
                                                   group_name=group_name)

    def run_resharding_send_task(self, uuid, buf_uuids):
        task: ReshardingSendTask = self.send_tasks[uuid]
        for send_tile_spec, buf_uuid in zip(task.tile_specs, buf_uuids):
            send_tile_spec: ReshardingTileSpec
            self.send_tile(buf_uuid, send_tile_spec.offset, send_tile_spec.rank,
                           send_tile_spec.gpu_idx, task.group_name)

    def run_resharding_recv_task(self, uuid, buf_uuids, set_empty_buffer=True):
        task: ReshardingRecvTask = self.recv_tasks[uuid]
        for recv_spec, buf_uuid in zip(task.recv_specs, buf_uuids):
            recv_spec: ReshardingRecvSpec
            if set_empty_buffer:
                self.put_non_zero_buffer(buf_uuid, recv_spec.device_id,
                                         recv_spec.shape, recv_spec.dtype)
            for recv_tile_spec in recv_spec.tile_specs:
                recv_tile_spec: ReshardingTileSpec
                self.recv_tile(buf_uuid, recv_spec.device_id,
                               recv_tile_spec.offset, recv_tile_spec.rank,
                               recv_tile_spec.gpu_idx, task.group_name)

    def put_resharding_allgather_task(self, uuid, tasks):
        all_gather_task = ReshardingAllGatherTask(tasks)
        allgather_specs = all_gather_task.allgather_specs
        for group_idx in allgather_specs:
            allgather_spec: ReshardingAllGatherSpec = allgather_specs[group_idx]
            device_ids = sorted(allgather_spec.device_ids)
            if repr(device_ids) not in self.allgather_communicators:
                communicators = nccl.NcclCommunicator.initAll(list(device_ids))
                self.allgather_communicators[repr(device_ids)] = communicators
        self.allgather_tasks[uuid] = all_gather_task

    def run_allgather_task(self, uuid, buffer_uuids):
        task: ReshardingAllGatherTask = self.allgather_tasks[uuid]
        allgather_specs = task.allgather_specs
        for group_idx in allgather_specs:
            allgather_spec: ReshardingAllGatherSpec = allgather_specs[group_idx]
            self.allgather(buffer_uuids, allgather_spec.device_ids,
                           allgather_spec.tensor_slices)
        return

    ##### Profiling Related Functions #####
    def profile_hlo_ops(self, op_infos: Sequence[Any], cache_filename: str,
                        single_timeout: float):
        num_devices = self.num_hosts * len(self.local_devices)
        return mesh_profiling.profile_hlo_ops(op_infos, self.backend,
                                              self.local_devices, self.host_id,
                                              num_devices, cache_filename,
                                              single_timeout)

    def profile_executable_with_dummy_inputs(self, uuid: int, **kwargs):
        return self.executables[uuid].profile_with_dummy_inputs(
            self.backend, self.local_devices, **kwargs)

    # TODO(yonghao): the sync function should be carefully reconsidered
    def profile_resharding_send_task(self,
                                     uuid,
                                     buf_uuids,
                                     warmup=1,
                                     repeat=3,
                                     number=3,
                                     sync=False):
        run_fn = lambda: self.run_resharding_send_task(uuid, buf_uuids)
        sync_fn = self.sync if sync else None
        costs = benchmark_func(run_fn, sync_fn, warmup, repeat, number)
        return np.mean(costs)

    def profile_resharding_recv_task(self,
                                     uuid,
                                     buf_uuids,
                                     warmup=1,
                                     repeat=3,
                                     number=3,
                                     sync=False):
        set_empty_buffer = True

        def run_fn():
            nonlocal set_empty_buffer
            self.run_resharding_recv_task(uuid, buf_uuids, set_empty_buffer)
            set_empty_buffer = False

        sync_fn = self.sync if sync else None
        costs = benchmark_func(run_fn, sync_fn, warmup, repeat, number)
        return np.mean(costs)

    def get_timer(self, name: str):
        return timers(name)

    def reset_timer(self, name: str):
        timers(name).reset()
        return True

    ##### Other Functions #####
    def sync(self):
        for device in self.local_devices:
            device.synchronize_all_activity()

    @staticmethod
    def check_alive():
        return 0

    def shutdown(self):
        self.sync()
        del self.buffers
        del self.executables
        self.distributed_client.shutdown()

    def destroy_collective_group(self, group_name: str = "default"):
        col.destroy_collective_group(group_name)


used_port_set = set((None,))


class PhysicalDeviceMesh:
    """
    A physical device mesh to run computation distributedly.

    This can be either a single-host device mesh (by using the native XLA runtime) or
    a multi-host device mesh (by using ray actors and the distributed XLA runtime).
    """

    def __init__(self,
                 devices=None,
                 host_ids: Sequence[int] = None,
                 host_info: Sequence[dict] = None,
                 head_ip: str = None,
                 num_devices_per_host: int = 1,
                 use_ray: bool = False):
        # actually we can infer use_ray by checking ip addresses.
        self.use_ray = use_ray
        self.host_ids = host_ids
        self.host_info = host_info
        self.head_ip = head_ip
        self.num_devices_per_host = num_devices_per_host
        self.workers = None
        self.launched = False

        # Do some argument check
        if not use_ray and not devices:
            raise RuntimeError(
                "`devices` are required for single-host device mesh.")
        # if devices and use_ray:
        #     raise RuntimeError("`devices` should not be passed in when using a Ray cluster.")
        if not use_ray:
            self.devices = devices
            self.host_ids = [0]
            self.host_info = None
            self.head_ip = "127.0.0.1"
            self.device_strs = [
                device_id_to_str(self.head_ip, d.id) for d in devices
            ]
            self.num_devices_per_host = len(self.devices)

        if use_ray:
            self.device_strs = []
            if devices is not None:
                if len(devices) != len(host_ids):
                    raise RuntimeError(
                        "Please specify the gpu IDs used on each host.")
                if not all(len(ids) == num_devices_per_host for ids in devices):
                    raise RuntimeError(
                        "Devices specified for each host does not align "
                        "with `num_devices_per_host`.")
            else:
                devices = [
                    list(range(num_devices_per_host))
                    for i, _ in enumerate(host_ids)
                ]
            self.devices = devices
            for i, _ in enumerate(self.host_ids):
                ip = self.host_info[i]["NodeManagerAddress"]
                self.device_strs.extend([
                    device_id_to_str(ip, i)
                    for devices_this_host in self.devices
                    for i in devices_this_host
                ])
            self._launch_xla_servers()

    @property
    def host_ips(self):
        """Return the a list containing all host IPs."""
        ips = [
            self.host_info[i]["NodeManagerAddress"]
            for i, _ in enumerate(self.host_ids)
        ]
        return ips

    def _launch_xla_servers(self):
        # Launch distributed xla runtime
        port = None
        while port in used_port_set:
            port = np.random.randint(20000, 25000)
        used_port_set.add(port)

        self.server_address = f"{self.head_ip}:{port}"
        self.service_server = None
        logger.debug(f"Trying to start XLA gRPC server on port: {port}...")
        self.service_server = xla_client._xla.get_distributed_runtime_service(
            self.server_address, self.num_hosts)
        logger.debug(f"Success to start XLA gRPC server on port: {port}...")
        time.sleep(0.5)

        # Launch workers
        self.workers = []
        for i in range(self.num_hosts):
            # Set XLA environment variables
            env_vars = {
                "PARAX_IS_WORKER": "True",
                "NCCL_USE_MULTISTREAM": "False",
                "XLA_PYTHON_CLIENT_MEM_FRACTION": str(
                    global_config.xla_client_mem_fraction),
                #"NCCL_LAUNCH_MODE": "PARALLEL",
                #"XLA_FLAGS": "--xla_dump_to=hlo --xla_dump_hlo_pass_re=.*"
                # "XLA_PYTHON_CLIENT_PREALLOCATE": "False",  # Note(Hao): remove this
                # "NCCL_SHM_DISABLE": "1",
                #"NCCL_DEBUG": "INFO" if i == 0 else "VERSION",
                # "CUDA_VISIBLE_DEVICES": ",".join([str(d) for d in self.device_ids[i]]),
                # "BETTER_EXCEPTIONS": "1",
                # "RAY_IGNORE_UNHANDLED_ERRORS": "True",
            }

            # Launch a ray actor
            node_resource = "node:" + self.host_info[i]["NodeManagerAddress"]
            cls = ray.remote(num_gpus=self.num_devices_per_host,
                             resources={node_resource: 1e-3})(MeshHostWorker)
            worker = cls.options(runtime_env={
                "env_vars": env_vars
            }).remote(self.server_address, self.num_hosts, i)
            self.workers.append(worker)
        self.sync_workers()
        self.launched = True

    def launch_xla_servers(self):
        assert not self.launched
        self._launch_xla_servers()

    def get_signature(self) -> str:
        """Return a signature string that contains the mesh shape and GPU model."""
        gpu_type = list_gpu_info()
        gpu_name = gpu_type.split("\n")[0].split(" (UUID:")[0][7:]
        ret = f"{len(self.host_ids)},{self.num_devices_per_host},{gpu_name}"
        ret = ret.replace(" ", "-")
        return ret

    @property
    def shape(self):
        return (len(self.host_ids), self.num_devices_per_host)

    @property
    def num_devices(self):
        """Return the total number of GPUs on this mesh."""
        return len(self.device_ids_flat)

    @property
    def num_hosts(self):
        """Return the number of hosts in the mesh."""
        return len(self.host_ids)

    @property
    def device_ids(self):
        """Return the device ids (does not distinguish host IPs)."""
        if not self.use_ray:
            return [self.devices]
        else:
            return self.devices

    @property
    def device_ids_flat(self):
        """Return the flattened device ids (do not distinguish host IPs)."""
        ids = [
            id for device_ids_this_host in self.device_ids
            for id in device_ids_this_host
        ]
        return ids

    @property
    def is_distributed(self):
        """Whether this mesh should be considered as a distributed mesh."""
        return not (self.num_hosts == 1 and not self.use_ray)

    ##### Mesh Related Functions #####
    def get_logical_mesh(self,
                         mesh_shape,
                         mesh_alpha=None,
                         mesh_beta=None,
                         mesh_topology=None,
                         intra_host_bandwidth=None,
                         inter_host_bandwidth=None):
        """Return a logical mesh and parameters of the alpha-beta communication cost model."""
        id_mesh = np.arange(self.num_devices).reshape(mesh_shape)

        if mesh_topology is None:
            mesh_alpha = mesh_alpha or (1,) * len(mesh_shape)
            mesh_beta = mesh_beta or (1,) * len(mesh_shape)
        elif mesh_topology == "tree":
            assert mesh_alpha is None
            assert mesh_beta is None
            mesh_alpha = [1] * 2
            mesh_beta = [None] * 2
            host_ids = np.tile(
                np.arange(self.num_hosts).reshape(-1, 1),
                self.num_devices_per_host)
            host_ids = host_ids.reshape(mesh_shape)

            # Compute bandwidth of doing communication along dim 0.
            # 1. Compute the number of links between each host pairs.
            #    Assume using ring-based algorithms.
            host_link_ct = defaultdict(int)
            for j in range(mesh_shape[1]):
                for i in range(mesh_shape[0]):
                    left = host_ids[i][j]
                    right = host_ids[(i + 1) % mesh_shape[0]][j]
                    if left != right:
                        if left > right:
                            left, right = right, left
                        host_link_ct[(left, right)] += 1

            j = 0
            # 2. Bandwidth between two hosts = total_bandwidth / number_of_links.
            #    Bandwdith along a communication dimension = min bandwidth of all links.
            bandwidth = intra_host_bandwidth
            for i in range(mesh_shape[0]):
                left = host_ids[i][j]
                right = host_ids[(i + 1) % mesh_shape[0]][j]
                if left != right:
                    if left > right:
                        left, right = right, left
                    bandwidth = min(
                        bandwidth,
                        inter_host_bandwidth / host_link_ct[(left, right)])
            mesh_beta[0] = 1 / bandwidth

            # Compute bandwidth of doing communication along dim 1.
            host_link_ct = defaultdict(int)
            for i in range(mesh_shape[0]):
                for j in range(mesh_shape[1]):
                    left = host_ids[i][j]
                    right = host_ids[i][(j + 1) % mesh_shape[1]]
                    if left != right:
                        if left > right:
                            left, right = right, left
                        host_link_ct[(left, right)] += 1

            i = 0
            bandwidth = intra_host_bandwidth
            for j in range(mesh_shape[1]):
                left = host_ids[i][j]
                right = host_ids[i][(j + 1) % mesh_shape[1]]
                if left != right:
                    if left > right:
                        left, right = right, left
                    bandwidth = min(
                        bandwidth,
                        inter_host_bandwidth / host_link_ct[(left, right)])
            mesh_beta[1] = 1 / bandwidth

        return LogicalDeviceMesh(self, id_mesh, mesh_alpha, mesh_beta)

    def get_default_logical_mesh(self):
        """Return the default logical mesh."""
        if self.num_hosts == 1:
            return self.get_logical_mesh(
                (self.num_hosts, self.num_devices_per_host), [1, 1], [1, 1])
        else:
            return self.get_logical_mesh(
                (self.num_hosts, self.num_devices_per_host), [1, 1], [1, 0.01])

    ##### Buffer Related Functions #####
    def get_remote_buffers(self, buf_refs: List[RemoteBufferRef]):
        """Get values of remote buffers."""
        obj_refs = []
        for buf_ref in buf_refs:
            obj_refs.append(self.workers[buf_ref.host_id].get_buffers.remote(
                buf_ref.uuid))

        return ray.get(obj_refs)

    def delete_remote_buffers(self, buf_refs: List[RemoteBufferRef]):
        """Delete remote buffers."""
        if not ray:
            return
        if self.workers is None or not ray.is_initialized():
            return

        for buf_ref in buf_refs:
            self.workers[buf_ref.host_id].delete_buffers.remote(buf_ref.uuid)

    def block_until_ready_remote_buffers(self, buf_refs: List[RemoteBufferRef]):
        """Block until the remote buffers are ready."""
        tasks = []
        for buf_ref in buf_refs:
            tasks.append(
                self.workers[buf_ref.host_id].block_until_ready_buffers.remote(
                    buf_ref.uuid))
        ray.get(tasks)

    ##### Executable Related Functions #####
    def shard_args(self, arg_indices: Sequence[Sequence[Index]],
                   donated_invars: Sequence[bool], args):
        """Shard the high-level arguments into low-level buffers."""
        if self.is_distributed:
            input_bufs = []
            for arg, indices, donated in zip(args, arg_indices, donated_invars):
                # Fast path for DistributedArray
                if isinstance(arg, DistributedArray) and arg.indices == indices:
                    input_bufs.append(arg.remote_buffers)
                elif isinstance(arg, ReplicatedDistributedArray):
                    replica = arg.get_replica_on_mesh(self)
                    assert replica.indices == indices
                    input_bufs.append(replica.remote_buffers)
                else:  # Slow path
                    # FIXME(yonghao): by get memory allocated there is an implicit sync between driver
                    # and worker devices. If not so, the total memory allocated drastically increases.
                    expected = (np.prod(arg.shape) *
                                np.dtype(arg.dtype).itemsize if hasattr(
                                    arg, "shape") else 1)
                    before_memory_usage = ray.get(
                        self.workers[0].get_memory_allocated.remote())
                    before_memory_peak = ray.get(
                        self.workers[0].get_max_memory_allocated.remote())
                    if type(arg) not in [ShapedArray, ShapeDtypeStruct]:
                        arg = xla.canonicalize_dtype(arg)
                    buf_refs = shard_arg_handlers[type(arg)](arg, self, indices)
                    input_bufs.append(buf_refs)
                    if donated and hasattr(arg, "delete"):
                        # shard_arg_handler always creates new buffers,
                        # so we can delete the old buffers
                        arg.delete()
                    after_memory_usage = ray.get(
                        self.workers[0].get_memory_allocated.remote())
                    after_memory_peak = ray.get(
                        self.workers[0].get_max_memory_allocated.remote())
                    actual = after_memory_usage - before_memory_usage
                    # print("Arg expected: {}, actual: {}, before usage: {}, after usage: {}".format(
                    #     expected, actual, before_memory_usage/1024**3, after_memory_usage/1024**3
                    # ))

            return input_bufs
        else:
            # single host w/o Ray
            return pxla.shard_args(self.devices, arg_indices, args)

    def get_outputs_handler(self, avals: Sequence[ShapedArray],
                            sharding_specs: Sequence[ShardingSpec]):
        """Get a function that wraps low-level buffers to high-level output arrays."""
        if self.is_distributed:
            indices = [
                pxla.spec_to_indices(aval.shape, spec)
                for aval, spec in zip(avals, sharding_specs)
            ]

            def outs_handler(bufs):
                ret = []
                for i, _ in enumerate(avals):
                    dis_array = DistributedArray(
                        device_mesh=self,
                        aval=avals[i],
                        sharding_spec=sharding_specs[i],
                        remote_buffers=bufs[i],
                        indices=indices[i])
                    ret.append(dis_array)
                return ret
        else:
            outs_handler = pxla.local_avals_to_results_handler(
                sharding_specs, avals)
        return outs_handler

    def delete_remote_executable(self, executable: MeshDriverExecutable):
        """Delete remote worker executables of a driver executable."""
        if self.workers is None or not ray.is_initialized():
            return

        for i in range(self.num_hosts):
            self.workers[i].delete_executable.remote(executable.exec_uuid)

    ##### Profiling related Functions #####
    def profile_hlo_ops(self,
                        op_infos: Sequence[Tuple],
                        cache_filename: str,
                        single_timeout: Optional[float] = None,
                        batch_timeout: Optional[float] = None):
        assert self.is_distributed
        tasks = []
        for w in self.workers:
            tasks.append(
                w.profile_hlo_ops.remote(op_infos, cache_filename,
                                         single_timeout))
        return ray.get(tasks, timeout=batch_timeout)[0]

    def get_remote_timer(self, timer_name: str):
        if self.is_distributed:
            return ray.get(self.workers[0].get_timer.remote(timer_name))
        else:
            return timers(timer_name)

    def reset_remote_timer(self, timer_name: str):
        if self.is_distributed:
            for worker in self.workers:
                ray.get(worker.reset_timer.remote(timer_name))
        else:
            timers(timer_name).reset()

    def get_memory_allocated(self):
        self.sync_workers()
        if self.is_distributed:
            return max(
                ray.get([w.get_memory_allocated.remote() for w in self.workers
                        ]))
        else:
            return max([d.memory_allocated() for d in self.devices])

    def get_max_memory_allocated(self):
        self.sync_workers()
        if self.is_distributed:
            return max(
                ray.get([
                    w.get_max_memory_allocated.remote() for w in self.workers
                ]))
        else:
            return max([d.max_memory_allocated() for d in self.devices])

    def get_available_memory(self):
        if self.is_distributed:
            return min(
                ray.get([w.get_available_memory.remote() for w in self.workers
                        ]))
        else:
            return min([device.available_memory() for device in self.devices])

    def reset_memory_stats(self):
        if self.is_distributed:
            for worker in self.workers:
                ray.get(worker.reset_memory_stats.remote())
        else:
            for device in self.devices:
                device.clear_memory_stats()

    ##### Other Functions #####
    def sync_workers(self):
        """Sync all device activities on workers."""
        if self.is_distributed:
            ray.get([w.sync.remote() for w in self.workers])
        else:
            for device in self.devices:
                device.synchronize_all_activity()

    def shutdown(self, forced=False):
        """Shut down the mesh."""
        if self.is_distributed:
            if not self.launched:
                return
            if not forced:
                ray.get([w.shutdown.remote() for w in self.workers])
            for worker in self.workers:
                ray.kill(worker)
            self.workers = None
            # shutdown grpc server
            self.service_server.shutdown()
            self.service_server = None
            self.launched = False
        else:
            self.sync_workers()


class DistributedArray:
    """A distributed array on a PhysicalDeviceMesh."""

    def __init__(self, device_mesh: PhysicalDeviceMesh, aval: ShapedArray,
                 sharding_spec: ShardingSpec,
                 remote_buffers: Sequence[RemoteBufferRef],
                 indices: Sequence[Index]):
        self.device_mesh = device_mesh
        self.aval = aval
        self.sharding_spec = sharding_spec
        self.remote_buffers = remote_buffers
        self.indices = indices

        self._npy_value = None
        self._one_replica_buffer_indices = None

    def block_until_ready(self):
        """Block until all remote buffers of this array are ready."""
        self.device_mesh.block_until_ready_remote_buffers(self.remote_buffers)

    def delete(self):
        for buf in self.remote_buffers:
            del buf
        self.device_buffers = None
        self._npy_value = None

    @property
    def one_replica_buffer_indices(self):
        """Indices of buffers containing one complete copy of the array data."""
        if self._one_replica_buffer_indices is None:
            one_replica_indices = []
            seen_index_hashes = OrderedSet()
            for i, index in enumerate(self.indices):
                hashed_index = _hashable_index(index)
                if hashed_index not in seen_index_hashes:
                    one_replica_indices.append(i)
                    seen_index_hashes.add(hashed_index)
            self._one_replica_buffer_indices = one_replica_indices
        return self._one_replica_buffer_indices

    @property
    def _value(self):
        if self._npy_value is None:
            npy_value = np.empty(self.aval.shape, self.aval.dtype)
            fetched_np_buffers = self.device_mesh.get_remote_buffers([
                self.remote_buffers[i] for i in self.one_replica_buffer_indices
            ])
            for ct, i in enumerate(self.one_replica_buffer_indices):
                npy_value[self.indices[i]] = fetched_np_buffers[ct]
            self._npy_value = npy_value
        return self._npy_value

    def __array__(self, dtype=None, context=None):
        return np.asarray(self._value, dtype=dtype)

    def __str__(self):
        return str(self._value)


core.pytype_aval_mappings[DistributedArray] = attrgetter('aval')
xla.pytype_aval_mappings[DistributedArray] = attrgetter('aval')
xla.canonicalize_dtype_handlers[DistributedArray] = lambda x: x


class ReplicatedDistributedArray:
    """A distributed array that is replicated on multiple meshes.

    We use this class as a workaround for symbols that type-change from DeviceArray
    to DistributedArray in pipeline-parallel training, such as optimizer's step.
    These variables do not have a resharding spec, and cannot be donated, but have a
    replica generates on every participant mesh.

    Warning: do not use this class unless you know exactly how.
    """

    def __init__(self, device_meshes: Sequence[PhysicalDeviceMesh],
                 arrays: Sequence[DistributedArray]):
        self._mesh_array_map = dict()
        self._array_mesh_map = dict()
        for mesh, array in zip(device_meshes, arrays):
            self._mesh_array_map[mesh] = array
            self._array_mesh_map[array] = mesh
        self.aval = self.replica.aval

    def is_replicated_on_mesh(self, mesh):
        """Whether this distributed array is on a given mesh."""
        if mesh in self._mesh_array_map:
            return True
        return False

    def get_replica_on_mesh(self, mesh):
        if not self.is_replicated_on_mesh(mesh):
            raise RuntimeError("No replica found on this mesh.")
        return self._mesh_array_map[mesh]

    def add_replica(self, mesh, array):
        assert isinstance(array, DistributedArray)
        assert isinstance(mesh, PhysicalDeviceMesh)
        if array in self._array_mesh_map:
            raise RuntimeError("Replica exists.")
        if mesh in self._mesh_array_map:
            raise RuntimeError("Mesh exists.")
        self._mesh_array_map.update({mesh: array})
        self._array_mesh_map.update({array: mesh})

    @property
    def replica(self):
        return list(self._mesh_array_map.values())[0]

    @property
    def _value(self):
        return self.replica._value

    def __array__(self, dtype=None, context=None):
        return np.asarray(self._value, dtype=dtype)


core.pytype_aval_mappings[ReplicatedDistributedArray] = attrgetter('aval')
xla.pytype_aval_mappings[ReplicatedDistributedArray] = attrgetter('aval')
xla.canonicalize_dtype_handlers[ReplicatedDistributedArray] = lambda x: x


class VirtualPhysicalMesh:
    """
    A virtual physical mesh used for pipeline parallel compilation.

    VirtualPhysicalMesh is used during compile time. We don't allocate actual workers for it.
    When compilation is finished, we instantiated it as a PhysicalDeviceMesh and launch workers.

    A VirtualPhysicalMesh can also be sliced into multiple VirtualPhysicalMesh.
    """

    def __init__(self,
                 host_ids: Sequence[int] = None,
                 host_info: Sequence[dict] = None,
                 head_ip: str = None,
                 num_devices_per_host: int = 1,
                 devices=None):
        self.host_ids = host_ids
        self.host_info = host_info
        self.head_ip = head_ip
        self.num_devices_per_host = num_devices_per_host

        if devices is not None:
            if len(devices) != len(host_ids):
                raise RuntimeError(
                    "Please specify the gpu IDs used on each host.")
            if not all(len(ids) == num_devices_per_host for ids in devices):
                raise RuntimeError(
                    "Device IDs specified for each host does not align "
                    "with `num_devices_per_host`.")
        else:
            devices = [list(range(num_devices_per_host)) for _ in host_ids]

        self.devices = devices
        # Depending on gpu_ids, generate device strs and ask Ray to allocate.
        self.device_strs = []
        for i, _ in enumerate(self.host_ids):
            ip = self.host_info[i]["NodeManagerAddress"]
            self.device_strs.extend([
                device_id_to_str(ip, i)
                for devices_this_host in devices
                for i in devices_this_host
            ])

    def slice_1d(self, dim, indices):
        """
        Slice a mesh given the slicing config.

        Args:
            dim (int): which dimension to slice from, 0 is host or 1 is the gpu
            indices (List[int]): indices to include along this dimension.

        Returns:
            mesh (PhysicalDeviceMesh)
        """
        if dim == 0:
            # slicing along the host dimension
            host_ids = [self.host_ids[x] for x in indices]
            host_info = [self.host_info[x] for x in host_ids]
            return VirtualPhysicalMesh(
                host_ids=host_ids,
                host_info=host_info,
                head_ip=self.head_ip,
                num_devices_per_host=self.num_devices_per_host)
        else:
            # slicing along the device dimension
            return VirtualPhysicalMesh(host_ids=self.host_ids,
                                       host_info=self.host_info,
                                       head_ip=self.head_ip,
                                       num_devices_per_host=len(indices[0]),
                                       devices=indices)

    def slice_2d(self, host_indices, device_indices):
        host_ids = [self.host_ids[x] for x in host_indices]
        host_info = [self.host_info[x] for x in host_indices]
        return VirtualPhysicalMesh(host_ids=host_ids,
                                   host_info=host_info,
                                   head_ip=self.head_ip,
                                   num_devices_per_host=len(device_indices[0]),
                                   devices=device_indices)

    def slice_profiling_submeshes(self, submesh_num_hosts,
                                  submesh_num_devices_per_host):
        num_hosts = len(self.host_ids)
        num_devices_per_host = self.num_devices_per_host
        num_host_submeshes = num_hosts // submesh_num_hosts
        num_device_submeshes = num_devices_per_host // submesh_num_devices_per_host
        all_submeshes = []
        for i in range(num_host_submeshes):
            for j in range(num_device_submeshes):
                host_indices = range(i * submesh_num_hosts,
                                     (i + 1) * submesh_num_hosts)
                device_indices = [
                    range(j * submesh_num_devices_per_host,
                          (j + 1) * submesh_num_devices_per_host)
                    for _ in host_indices
                ]
                all_submeshes.append(self.slice_2d(host_indices,
                                                   device_indices))
        return all_submeshes

    @property
    def shape(self):
        return (len(self.host_ids), self.num_devices_per_host)

    @property
    def num_devices(self):
        """Return the total number of GPUs on this mesh."""
        return len(self.device_ids_flat)

    @property
    def num_hosts(self):
        """Return the number of hosts in the mesh."""
        return len(self.host_ids)

    @property
    def device_ids(self):
        """Return the device ids (does not distinguish host IPs)."""
        return self.devices

    @property
    def device_ids_flat(self):
        """Return the flattened device ids (do not distinguish host IPs)."""
        ids = [
            id for device_ids_this_host in self.device_ids
            for id in device_ids_this_host
        ]
        return ids

    @property
    def is_distributed(self):
        """Whether this mesh should be considered as a distributed mesh."""
        return True

    def get_physical_mesh(self):
        """Convert to a physical mesh (which will request resources from Ray)."""
        return PhysicalDeviceMesh(
            host_ids=self.host_ids,
            host_info=self.host_info,
            head_ip=self.head_ip,
            num_devices_per_host=self.num_devices_per_host,
            devices=self.device_ids,
            use_ray=True)

    def get_logical_mesh(self, mesh_shape, mesh_alpha=None, mesh_beta=None):
        """Generate a logical mesh."""
        id_mesh = np.arange(self.num_devices).reshape(mesh_shape)
        mesh_alpha = mesh_alpha or (1.0,) * len(mesh_shape)
        mesh_beta = mesh_beta or (1.0,) * len(mesh_shape)
        return LogicalDeviceMesh(self, id_mesh, mesh_alpha, mesh_beta)

    def get_default_logical_mesh(self):
        """Return the default logical mesh."""
        if self.num_hosts == 1:
            return self.get_logical_mesh(
                (self.num_hosts, self.num_devices_per_host), [1, 1], [1, 1])
        else:
            return self.get_logical_mesh(
                (self.num_hosts, self.num_devices_per_host), [1, 1], [1, 0.1])

    def get_1d_logical_mesh(self):
        """Return a 1D logical mesh."""
        return self.get_logical_mesh((1, self.num_devices))


class DeviceCluster:
    """A ray cluster with GPU devices."""

    def __init__(self):
        # pylint: disable=import-outside-toplevel
        from ray.worker import _global_node as ray_global_node
        self.head_info = ray_global_node.address_info
        self.head_ip = self.head_info['node_ip_address']

        # Gather host ids
        self.host_info = []
        for node in ray.nodes():
            for key in node["Resources"]:
                if key.startswith("node:"):
                    self.host_info.append(node)

        # Gather device info
        self.num_devices = []
        for host_info in self.host_info:
            number = host_info["Resources"]["GPU"]
            assert number.is_integer()
            self.num_devices.append(int(number))

    @property
    def num_cpus(self):
        return sum(
            map(lambda info: int(info["Resources"]["CPU"]), self.host_info))

    def get_physical_mesh(self,
                          host_ids: Sequence[int] = None,
                          num_devices_per_host: int = None):
        """
        Slice a subset of hosts and devices to form a physical device mesh.

        Args:
            host_ids: The index of host nodes.
                'None' means using all hosts
            num_devices_per_host: The number of devices per host.
                'None' means using all devices

        Return:
            A physical multi-host device mesh
        """
        host_ids = host_ids or np.arange(len(self.host_info))
        host_info = [self.host_info[x] for x in host_ids]

        num_devices_per_host = num_devices_per_host or self.num_devices[
            host_ids[0]]
        for host_id in host_ids:
            assert self.num_devices[host_id] >= num_devices_per_host

        return PhysicalDeviceMesh(host_ids=host_ids,
                                  host_info=host_info,
                                  num_devices_per_host=num_devices_per_host,
                                  head_ip=self.head_ip,
                                  use_ray=True)

    def get_virtual_physical_mesh(self,
                                  host_ids: Sequence[int] = None,
                                  num_devices_per_host: int = None):
        """
        Slice a subset of hosts and devices to form a virtual physical mesh.

        The only difference between a virtual and a physical mesh is that a virtual
        mesh does not request cluster resources.
        """
        host_ids = host_ids or np.arange(len(self.host_info))
        host_info = [self.host_info[x] for x in host_ids]

        num_devices_per_host = num_devices_per_host or self.num_devices[
            host_ids[0]]
        for host_id in host_ids:
            assert self.num_devices[host_id] >= num_devices_per_host

        return VirtualPhysicalMesh(host_ids=host_ids,
                                   host_info=host_info,
                                   num_devices_per_host=num_devices_per_host,
                                   head_ip=self.head_ip)

    def profile_all(self, *args, **kwargs):
        """Profile computation and communication cost for all submesh shapes of this cluster."""
        return mesh_profiling.profile_all(self, *args, **kwargs)


########################################
# Register ShardArg Handler
########################################
def _device_mesh_put(device_mesh, shards):
    # Put shards to the distributed device
    buf_refs = []
    pt = 0
    for host_id in range(device_mesh.num_hosts):
        for device_id in range(device_mesh.num_devices_per_host):
            buf_ref = RemoteBufferRef(device_mesh,
                                      host_id,
                                      device_id,
                                      dtype=shards[pt].dtype)
            if global_config.use_dummy_value_for_benchmarking:
                device_mesh.workers[host_id].put_non_zero_buffer.remote(
                    buf_ref.uuid, device_id, shards[pt].shape, shards[pt].dtype)
            else:
                device_mesh.workers[host_id].put_buffer.remote(
                    buf_ref.uuid, device_id, shards[pt])
            buf_refs.append(buf_ref)
            pt += 1
    return buf_refs


def _shard_array(x, device_mesh, indices):
    # Create shards according to indices for a numpy array
    return _device_mesh_put(device_mesh, [x[i] for i in indices])


def _shard_abstract_array(x, device_mesh, indices):
    # Create shards according to indices for ShapedArray or ShapeDtypeStruct
    # Note: ShapedArray and ShapeDtypeStruct only works when
    #   global_config.use_dummy_value_for_benchmarking is True
    assert global_config.use_dummy_value_for_benchmarking
    shards = []
    for index in indices:
        assert len(index) == len(x.shape)
        shard_shape = []
        for i, s in enumerate(index):
            filled_slice = s.indices(x.shape[i])
            dim_size = len(range(*filled_slice))
            shard_shape.append(dim_size)
        shard = x.__class__(shape=tuple(shard_shape), dtype=x.dtype)
        shards.append(shard)
    return _device_mesh_put(device_mesh, shards)


def _shard_device_array(array, device_mesh, indices):
    # Create shards according to indices for a DeviceArray
    if global_config.use_dummy_value_for_benchmarking:
        start_indices, limit_indices, removed_dims = map(
            tuple, unzip3(_as_slice_indices(array, idx) for idx in indices))

        def slice_func():
            return array._multi_slice(start_indices, limit_indices,
                                      removed_dims)

        shards = eval_shape(slice_func)
    else:
        start_indices, limit_indices, removed_dims = map(
            tuple, unzip3(_as_slice_indices(array, idx) for idx in indices))
        shards = array._multi_slice(start_indices, limit_indices, removed_dims)

    return _device_mesh_put(device_mesh, shards)


def _shard_distributed_array(array, device_mesh, indices):
    # Create shards according to indices for a DistributedArray
    return shard_arg_handlers[type(array._value)](array._value, device_mesh,
                                                  indices)


shard_arg_handlers = {}  # Shard an argument to a distributed device mesh
for t in array_types:
    shard_arg_handlers[t] = _shard_array
shard_arg_handlers[ShapedArray] = _shard_abstract_array
shard_arg_handlers[ShapeDtypeStruct] = _shard_abstract_array
shard_arg_handlers[xla._DeviceArray] = _shard_device_array
shard_arg_handlers[xla._CppDeviceArray] = _shard_device_array
shard_arg_handlers[DistributedArray] = _shard_distributed_array
shard_arg_handlers[ShardedDeviceArray] = _shard_distributed_array