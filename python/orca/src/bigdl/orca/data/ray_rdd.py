#
# Copyright 2018 Analytics Zoo Authors.
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
#
from collections import defaultdict

import ray
import ray.services
import uuid
import random

from zoo.ray import RayContext


class PartitionUploader:

    def __init__(self):
        self.partitions = {}

    def upload_partition(self, idx, partition):
        self.partitions[idx] = partition
        return 0

    def upload_shards(self, shard_id, shard):
        self.partitions[shard_id] = shard
        return 0

    def get_partitions(self):
        return self.partitions

    def get_partition(self, idx):
        return self.partitions[idx]

    def upload_multiple_partitions(self, idxs, partitions):
        for idx, partition in zip(idxs, partitions):
            self.upload_partition(idx, partition)
        return 0

    def upload_partitions_from_plasma(self, partition_id, plasma_object_id, object_store_address):
        import pyarrow.plasma as plasma
        client = plasma.connect(object_store_address)
        partition = client.get(plasma_object_id)
        self.partitions[partition_id] = partition
        return 0


def write_to_ray(idx, partition, redis_address, redis_password, partition_store_names):
    partition = list(partition)
    ray.init(address=redis_address, redis_password=redis_password, ignore_reinit_error=True)
    ip = ray.services.get_node_ip_address()
    local_store_name = None
    for name in partition_store_names:
        if name.endswith(ip):
            local_store_name = name
            break
    if local_store_name is None:
        local_store_name = random.choice(partition_store_names)

    local_store = ray.util.get_actor(local_store_name)

    # directly calling ray.put will set this driver as the owner of this object,
    # when the spark job finished, the driver might exit and make the object
    # eligible for deletion.
    ray.get(local_store.upload_partition.remote(idx, partition))
    ray.shutdown()

    return [(idx, local_store_name.split(":")[-1], local_store_name)]


def get_from_ray(idx, redis_address, redis_password, idx_to_store_name):
    if not ray.is_initialized():
        ray.init(address=redis_address, redis_password=redis_password, ignore_reinit_error=True)
    local_store_handle = ray.util.get_actor(idx_to_store_name[idx])
    partition = ray.get(local_store_handle.get_partition.remote(idx))
    ray.shutdown()
    return partition


class RayRdd:

    def __init__(self, uuid, partition2store_name, partition2ip, partition_stores):
        self.uuid = uuid
        self.partition2store_name = partition2store_name
        self.partition2ip = partition2ip
        self.partition_stores = partition_stores

    def num_partitions(self):
        return len(self.partition2ip)

    def collect(self):
        partitions = self.collect_partitions()
        data = [item for part in partitions for item in part]
        return data

    def collect_partitions(self):
        part_refs = [local_store.get_partitions.remote()
                     for local_store in self.partition_stores.values()]
        partitions = ray.get(part_refs)

        result = {}
        for part in partitions:
            result.update(part)
        return [result[idx] for idx in range(self.num_partitions())]

    def to_spark_rdd(self):
        ray_ctx = RayContext.get()
        sc = ray_ctx.sc
        address = ray_ctx.redis_address
        password = ray_ctx.redis_password
        num_parts = self.num_partitions()
        partition2store = self.partition2store_name
        rdd = sc.parallelize([0] * num_parts * 10, num_parts)\
            .mapPartitionsWithIndex(
            lambda idx, _: get_from_ray(idx, address, password, partition2store))
        return rdd

    def _get_multiple_partition_refs(self, ids):
        refs = []
        for idx in ids:
            local_store_handle = self.partition_stores[self.partition2store_name[idx]]
            partition_ref = local_store_handle.get_partition.remote(idx)
            refs.append(partition_ref)
        return refs

    def map_partitions_with_actors(self, actors, func, gang_scheduling=True):
        assigned_partitions, actor_ips = self.assign_partitions_to_actors(actors,
                                                                          gang_scheduling)
        assigned_partition_refs = [(part_ids, self._get_multiple_partition_refs(part_ids))
                                   for part_ids in assigned_partitions]
        new_part_id_refs = {part_id: func(actor, part_ref)
                            for actor, (part_ids, part_refs) in zip(actors, assigned_partition_refs)
                            for part_id, part_ref in zip(part_ids, part_refs)}

        actor_ip2part_id = defaultdict(list)
        for actor_ip, part_ids in zip(actor_ips, assigned_partitions):
            actor_ip2part_id[actor_ip].extend(part_ids)

        return RayRdd.from_partition_refs(actor_ip2part_id, new_part_id_refs)

    def zip_partitions_with_actors(self, ray_rdd, actors, func, gang_scheduling=True):
        assert self.num_partitions() == ray_rdd.num_partitions(),\
            "the rdds to be zipped must have the same number of partitions"
        assigned_partitions, actor_ips = self.assign_partitions_to_actors(actors,
                                                                          gang_scheduling)
        new_part_id_refs = {}
        for actor, part_ids in zip(actors, assigned_partitions):
            assigned_partition_refs = self._get_multiple_partition_refs(part_ids)
            assigned_partition_refs_other = ray_rdd._get_multiple_partition_refs(part_ids)
            for part_id, this_part_ref, that_part_ref in \
                    zip(part_ids, assigned_partition_refs, assigned_partition_refs_other):
                new_ref = func(actor, this_part_ref, that_part_ref)
                new_part_id_refs[part_id] = new_ref

        actor_ip2part_id = defaultdict(list)
        for actor_ip, part_ids in zip(actor_ips, assigned_partitions):
            actor_ip2part_id[actor_ip].extend(part_ids)

        return RayRdd.from_partition_refs(actor_ip2part_id, new_part_id_refs)

    def assign_partitions_to_actors(self, actors, one_to_one=True):
        num_parts = self.num_partitions()
        if num_parts < len(actors):
            raise ValueError(f"this rdd has {num_parts} partitions, which is smaller"
                             f"than actor number ({len(actors)} actors).")

        avg_part_num = num_parts // len(actors)
        remainder = num_parts % len(actors)

        if one_to_one:
            assert avg_part_num == 1 and remainder == 0,\
                "there must be the same number of actors and partitions," \
                f" got actor number: {len(actors)}, partition number: {num_parts}"

        part_id2ip = self.partition2ip.copy()
        # the assigning algorithm
        # 1. calculate the average partition number per actor avg_part_num, and the number
        #    of remaining partitions remainder. So there are remainder number of actors got
        #    avg_part_num + 1 partitions and other actors got avg_part_num partitions.
        # 2. loop partitions and assign each according to ip, if no actor with this ip or
        #    all actors with this ip have been full, this round of assignment failed.
        # 3. assign the partitions that failed to be assigned to actors that has full

        # todo extract this algorithm to other functions for unit tests.
        actor_ips = []
        for actor in actors:
            assert hasattr(actor, "get_node_ip"), "each actor should have a get_node_ip method"
            actor_ip = actor.get_node_ip.remote()
            actor_ips.append(actor_ip)

        actor_ips = ray.get(actor_ips)

        actor2assignments = [[] for i in range(len(actors))]

        ip2actors = {}
        for idx, ip in enumerate(actor_ips):
            if ip not in ip2actors:
                ip2actors[ip] = []
            ip2actors[ip].append(idx)

        unassigned = []
        for part_idx, ip in part_id2ip.items():
            assigned = False
            if ip in ip2actors:
                ip_actors = ip2actors[ip]

                for actor_id in ip_actors:
                    current_assignments = actor2assignments[actor_id]
                    if len(current_assignments) < avg_part_num:
                        current_assignments.append(part_idx)
                        assigned = True
                        break
                    elif len(current_assignments) == avg_part_num and remainder > 0:
                        current_assignments.append(part_idx)
                        remainder -= 1
                        assigned = True
                        break
            if not assigned:
                unassigned.append((part_idx, ip))

        for part_idx, ip in unassigned:
            for current_assignments in actor2assignments:
                if len(current_assignments) < avg_part_num:
                    current_assignments.append(part_idx)
                elif len(current_assignments) == avg_part_num and remainder > 0:
                    current_assignments.append(part_idx)
                    remainder -= 1
        return actor2assignments, actor_ips

    @staticmethod
    def from_partition_refs(ip2part_id, part_id2ref):
        ray_ctx = RayContext.get()
        uuid_str = str(uuid.uuid4())
        id2store_name = {}
        partition_stores = {}
        part_id2ip = {}
        result = []
        for node, part_ids in ip2part_id.items():
            name = f"partition:{uuid_str}:{node}"
            store = ray.remote(num_cpus=0, resources={f"node:{node}": 1e-4})(PartitionUploader) \
                .options(name=name).remote()
            partition_stores[name] = store
            for idx in part_ids:
                result.append(store.upload_partition.remote(idx, part_id2ref[idx]))
                id2store_name[idx] = name
                part_id2ip[idx] = node
        ray.get(result)
        return RayRdd(uuid_str, id2store_name, part_id2ip, partition_stores)

    @staticmethod
    def from_spark_rdd(rdd):
        return RayRdd._from_spark_rdd_ray_api(rdd)

    @staticmethod
    def _from_spark_rdd_ray_api(rdd):
        ray_ctx = RayContext.get()
        address = ray_ctx.redis_address
        password = ray_ctx.redis_password
        driver_ip = ray.services.get_node_ip_address()
        uuid_str = str(uuid.uuid4())
        resources = ray.cluster_resources()
        nodes = []
        for key, value in resources.items():
            if key.startswith("node:"):
                # if running in cluster, filter out driver ip
                if not (not ray_ctx.is_local and key == f"node:{driver_ip}"):
                    nodes.append(key)

        partition_stores = {}
        for node in nodes:
            name = f"partition:{uuid_str}:{node}"
            store = ray.remote(num_cpus=0, resources={node: 1e-4})(PartitionUploader)\
                .options(name=name).remote()
            partition_stores[name] = store
        partition_store_names = list(partition_stores.keys())
        result = rdd.mapPartitionsWithIndex(lambda idx, part: write_to_ray(
            idx, part, address, password, partition_store_names)).collect()
        id2ip = {idx: ip for idx, ip, _ in result}
        id2store_name = {idx: store for idx, _, store in result}

        return RayRdd(uuid_str, dict(id2store_name), dict(id2ip), partition_stores)
