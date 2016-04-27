"""Contains information for partition layout on given cluster.

Also contains api's dealing with changes in partition layout.
The steps (1-6) and states S0 -- S2 and algorithm for re-assigning-partitions
is per the design document at:-

https://docs.google.com/document/d/1qloANcOHkzuu8wYVm0ZAMCGY5Mmb-tdcxUywNIXfQFI
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

import logging
from collections import OrderedDict

from .broker import Broker
from .error import BrokerDecommissionError
from .error import InvalidBrokerIdError
from .partition import Partition
from .rg import ReplicationGroup
from .topic import Topic
from .util import compute_optimum
from .util import separate_groups


class ClusterTopology(object):
    """Represent a Kafka cluster and functionalities supported over the cluster.

        :param assignment: cluster assignment is a dict (topic, partition): replicas
        :param brokers: dict representing the active brokers of the
            cluster broker_id: metadata (metadata is the content of the zookeeper
            node of the broker)
        :param extract_group: function used to extract the replication group
            from each broker. The extract_group function is called for each
            broker passing the Broker object as argument. It should return a
            string representing the ReplicationGroup id.
    """

    def __init__(self, assignment, brokers, extract_group=lambda x: None):
        self.extract_group = extract_group
        self.log = logging.getLogger(self.__class__.__name__)
        self.topics = {}
        self.rgs = {}
        self.brokers = {}
        self.partitions = {}
        self._build_brokers(brokers)
        self._build_partitions(assignment)
        self.log.debug(
            'Total partitions in cluster {partitions}'.format(
                partitions=len(self.partitions),
            ),
        )
        self.log.debug(
            'Total replication-groups in cluster {rgs}'.format(
                rgs=len(self.rgs),
            ),
        )
        self.log.debug(
            'Total brokers in cluster {brokers}'.format(
                brokers=len(self.brokers),
            ),
        )

    def _build_brokers(self, brokers):
        """Build broker objects using broker-ids."""
        for broker_id, metadata in brokers.iteritems():
            self.brokers[broker_id] = self._create_broker(broker_id, metadata)

    def _create_broker(self, broker_id, metadata=None):
        """Create a broker object and assign to a replication group.
        A broker object with no metadata is considered inactive.
        An inactive broker may or may not belong to a group.
        """
        broker = Broker(broker_id, metadata)
        if not metadata:
            broker.mark_inactive()
        rg_id = self.extract_group(broker)
        group = self.rgs.setdefault(rg_id, ReplicationGroup(rg_id))
        group.add_broker(broker)
        broker.replication_group = group
        return broker

    def _build_partitions(self, assignment):
        """Builds all partition objects and update corresponding broker and
        topic objects.
        """
        self.partitions = {}
        for partition_name, replica_ids in assignment.iteritems():
            # Get topic
            topic_id = partition_name[0]
            partition_id = partition_name[1]
            topic = self.topics.setdefault(
                topic_id,
                Topic(topic_id, replication_factor=len(replica_ids))
            )

            # Creating partition object
            partition = Partition(topic, partition_id)
            self.partitions[partition_name] = partition
            topic.add_partition(partition)

            # Updating corresponding broker objects
            for broker_id in replica_ids:
                # Check if broker-id is present in current active brokers
                if broker_id not in self.brokers.keys():
                    self.log.warning(
                        "Broker %s containing partition %s is not in "
                        "active brokers.",
                        broker_id,
                        partition,
                    )
                    broker = self._create_broker(broker_id)
                else:
                    broker = self.brokers[broker_id]

                broker.add_partition(partition)

    @property
    def assignment(self):
        assignment = {}
        for partition in self.partitions.itervalues():
            assignment[
                (partition.topic.id, partition.partition_id)
            ] = [broker.id for broker in partition.replicas]
        # assignment map created in sorted order for deterministic solution
        return OrderedDict(sorted(assignment.items(), key=lambda t: t[0]))

    @property
    def partition_replicas(self):
        """Return list of all partitions in the cluster.
        Note: List contains all replicas as well.
        """
        return [
            partition
            for rg in self.rgs.itervalues()
            for partition in rg.partitions
        ]

    def rebalance_replication_groups(self):
        """Rebalance partitions over replication groups (availability-zones).

        First step involves rebalancing replica-count for each partition across
        replication-groups.
        Second step involves rebalancing partition-count across replication-groups
        of the cluster.
        """
        # Balance replicas over replication-groups for each partition
        for partition in self.partitions.itervalues():
            self._rebalance_partition(partition)

        # Balance partition-count over replication-groups
        self._rebalance_groups_partition_cnt()

    def decommission_brokers(self, broker_ids):
        """Decommission a list of brokers trying to keep the replication group
        the brokers belong to balanced.

        :param broker_ids: list of string representing valid broker ids in the cluster
        :raises: InvalidBrokerIdError when the id is invalid.
        """
        groups = set()
        for b_id in broker_ids:
            try:
                broker = self.brokers[b_id]
            except KeyError:
                self.log.error("Invalid broker id %s.", b_id)
                # Raise an error for now. As alternative we may ignore the
                # invalid id and continue with the others.
                raise InvalidBrokerIdError(
                    "Broker id {} does not exist in cluster".format(b_id),
                )
            broker.mark_decommissioned()
            groups.add(broker.replication_group)

        for group in groups:
            try:
                group.rebalance_brokers()
            except BrokerDecommissionError:
                # In this case we need to reassign the remaining partitions to
                # other replication groups
                failed = False
                for broker in group.brokers:
                    if broker.decommissioned and not broker.empty():
                        self.log.info(
                            "Broker: %s can't be decommissioned withing the same "
                            "replication group: %s. Moving partitions to other "
                            "replication groups.",
                            broker,
                            broker.replication_group,
                        )
                        self._force_broker_decommission(broker)
                    # Broker should be empty now
                    if not broker.empty():
                        self.log.error(
                            "Impossible to decommission broker %s partitions %s.",
                            broker,
                            broker.partitions,
                        )
                        failed = True
                if failed:
                    # Decommission may be impossible if there are not enough
                    # brokers to redistributed the replicas.
                    self.error("Broker decommission failed.")
                    raise BrokerDecommissionError(
                        "Broker decommission failed after force."
                    )

    def _force_broker_decommission(self, broker):
        available_groups = [
            rg for rg in self.rgs.itervalues()
            if rg is not broker.replication_group
        ]
        for partition in broker.partitions.itervalues:
            group = min(
                available_groups,
                key=lambda x: x.count_replica(partition),
            )
            self.log.debug(
                "Try to move partition: %s from broker %s to replication group %s",
                partition,
                broker,
                broker.replication_group,
            )
            group.acquire_partition(partition, broker)

    def _rebalance_partition(self, partition):
        """Rebalance replication group for given partition."""
        # Separate replication-groups into under and over replicated
        total = sum(
            group.count_replica(partition)
            for group in self.rgs.itervalues()
        )
        over_replicated_rgs, under_replicated_rgs = separate_groups(
            self.rgs.values(),
            lambda g: g.count_replica(partition),
            total,
        )
        # Move replicas from over-replicated to under-replicated groups
        while under_replicated_rgs and over_replicated_rgs:
            # Decide source and destination group
            rg_source = self._elect_source_replication_group(
                over_replicated_rgs,
                partition,
            )
            rg_destination = self._elect_dest_replication_group(
                rg_source.count_replica(partition),
                under_replicated_rgs,
                partition,
            )
            if rg_source and rg_destination:
                # Actual movement of partition
                self.log.debug(
                    'Moving partition {p_name} from replication-group '
                    '{rg_source} to replication-group {rg_dest}'.format(
                        p_name=partition.name,
                        rg_source=rg_source.id,
                        rg_dest=rg_destination.id,
                    ),
                )
                rg_source.move_partition(rg_destination, partition)
            else:
                # Groups balanced or cannot be balanced further
                break
            # Re-compute under and over-replicated replication-groups
            over_replicated_rgs, under_replicated_rgs = separate_groups(
                self.rgs.values(),
                lambda g: g.count_replica(partition),
                total,
            )

    def _elect_source_replication_group(
        self,
        over_replicated_rgs,
        partition,
    ):
        """Decide source replication-group based as group with highest replica
        count.
        """
        return max(
            over_replicated_rgs,
            key=lambda rg: rg.count_replica(partition),
        )

    def _elect_dest_replication_group(
        self,
        replica_count_source,
        under_replicated_rgs,
        partition,
    ):
        """Decide destination replication-group based on replica-count."""
        min_replicated_rg = min(
            under_replicated_rgs,
            key=lambda rg: rg.count_replica(partition),
        )
        # Locate under-replicated replication-group with lesser
        # replica count than source replication-group
        if min_replicated_rg.count_replica(partition) < replica_count_source - 1:
            return min_replicated_rg
        return None

    # Re-balancing partition count across brokers
    def _rebalance_groups_partition_cnt(self):
        """Re-balance partition-count across replication-groups.

        Algorithm:
        The key constraint is not to create any replica-count imbalance while
        moving partitions across replication-groups.
        1) Divide replication-groups into over and under loaded groups in terms
           of partition-count.
        2) For each over-loaded replication-group, select eligible partitions
           which can be moved to under-replicated groups. Partitions with greater
           than optimum replica-count for the group have the ability to donate one
           of their replicas without creating replica-count imbalance.
        3) Destination replication-group is selected based on minimum partition-count
           and ability to accept one of the eligible partition-replicas.
        4) Source and destination brokers are selected based on :-
            * their ability to donate and accept extra partition-replica respectively.
            * maximum and minimum partition-counts respectively.
        5) Move partition-replica from source to destination-broker.
        6) Repeat steps 1) to 5) until groups are balanced or cannot be balanced further.
        """
        # Segregate replication-groups based on partition-count
        total_elements = sum(len(rg.partitions) for rg in self.rgs.itervalues())
        over_loaded_rgs, under_loaded_rgs = separate_groups(
            self.rgs.values(),
            lambda rg: len(rg.partitions),
            total_elements,
        )
        if over_loaded_rgs and under_loaded_rgs:
            self.log.info(
                'Over-loaded replication-groups {over_loaded}, under-loaded '
                'replication-groups {under_loaded} based on partition-count'
                .format(
                    over_loaded=[rg.id for rg in over_loaded_rgs],
                    under_loaded=[rg.id for rg in under_loaded_rgs],
                )
            )
        else:
            self.log.info('Replication-groups are balanced based on partition-count.')
            return

        # Get optimal partition-count per replication-group
        opt_partition_cnt, _ = compute_optimum(
            len(self.rgs),
            total_elements,
        )
        # Balance replication-groups
        for over_loaded_rg in over_loaded_rgs:
            for under_loaded_rg in under_loaded_rgs:
                # Filter unique partition with replica-count > opt-replica-count
                # in over-loaded-rgs and <= opt-replica-count in under-loaded-rgs
                eligible_partitions = set(filter(
                    lambda partition:
                    over_loaded_rg.count_replica(partition) >
                        len(partition.replicas) // len(self.rgs) and
                    under_loaded_rg.count_replica(partition) <=
                        len(partition.replicas) // len(self.rgs),
                    over_loaded_rg.partitions,
                ))
                # Move all possible partitions
                for eligible_partition in eligible_partitions:
                    over_loaded_rg.move_partition_replica(
                        under_loaded_rg,
                        eligible_partition,
                    )
                    # Move to next replication-group if either of the groups got
                    # balanced, otherwise try with next eligible partition
                    if (len(under_loaded_rg.partitions) == opt_partition_cnt or
                            len(over_loaded_rg.partitions) == opt_partition_cnt):
                        break
                if len(over_loaded_rg.partitions) == opt_partition_cnt:
                    # Move to next over-loaded replication-group if balanced
                    break

    # Re-balancing partition count across brokers
    def rebalance_brokers(self):
        """Rebalance partition-count across brokers within each replication-group."""
        for rg in self.rgs.itervalues():
            rg.rebalance_brokers()

    # Re-balancing leaders
    def rebalance_leaders(self):
        """Re-order brokers in replicas such that, every broker is assigned as
        preferred leader evenly.
        """
        opt_leader_cnt = len(self.partitions) // len(self.brokers)
        # Balanced brokers transfer leadership to their under-balanced followers
        self.rebalancing_non_followers(opt_leader_cnt)

    def rebalancing_non_followers(self, opt_cnt):
        """Transfer leadership to any under-balanced followers on the pretext
        that they remain leader-balanced or can be recursively balanced through
        non-followers (followers of other leaders).

        Context:
        Consider a graph G:
        Nodes: Brokers (e.g. b1, b2, b3)
        Edges: From b1 to b2 s.t. b1 is a leader and b2 is its follower
        State of nodes:
            1. Over-balanced/Optimally-balanced: (OB)
                if leadership-count(broker) >= opt-count
            2. Under-balanced (UB) if leadership-count(broker) < opt-count
            leader-balanced: leadership-count(broker) is in [opt-count, opt-count+1]

        Algorithm:
            1. Use Depth-first-search algorithm to find path between
            between some UB-broker to some OB-broker.
            2. If path found, update UB-broker and delete path-edges (skip-partitions).
            3. Continue with step-1 until all possible paths explored.
        """
        under_brokers = filter(
            lambda b: b.count_preferred_replica() < opt_cnt,
            self.brokers.itervalues(),
        )
        if under_brokers:
            skip_brokers, skip_partitions = [], []
            for broker in under_brokers:
                skip_brokers.append(broker)
                broker.request_leadership(opt_cnt, skip_brokers, skip_partitions)

        over_brokers = filter(
            lambda b: b.count_preferred_replica() > opt_cnt + 1,
            self.brokers.itervalues(),
        )
        # Any over-balanced brokers tries to donate their leadership to followers
        if over_brokers:
            skip_brokers, used_edges = [], []
            for broker in over_brokers:
                skip_brokers.append(broker)
                broker.donate_leadership(opt_cnt, skip_brokers, used_edges)
