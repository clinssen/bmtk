# Copyright 2017. Allen Institute. All rights reserved
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the
# following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following
# disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following
# disclaimer in the documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote
# products derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
import os
import json
import functools
import nest

from six import string_types
import numpy as np

from bmtk.simulator.core.simulator_network import SimNetwork
from bmtk.simulator.pointnet.sonata_adaptors import PointNodeAdaptor, PointEdgeAdaptor
from bmtk.simulator.pointnet import pyfunction_cache
from bmtk.simulator.pointnet.io_tools import io
from bmtk.simulator.pointnet.nest_utils import nest_version
from .gids import GidPool


def set_spikes_nest2(node_id, nest_obj, spike_trains):
    st = spike_trains.get_times(node_id=node_id)
    if st is None or len(st) == 0:
        return

    st = np.array(st)
    if np.any(st <= 0.0):
        # NRN will fail if VecStim contains negative spike-time, throw an exception and log info for user
        io.log_exception('spike train {} contains negative/zero time, unable to run virtual cell in NEST'.format(st))
    st.sort()
    nest.SetStatus([nest_obj], {'spike_times': st})


def set_spikes_nest3(node_id, nest_obj, spike_trains):
    st = spike_trains.get_times(node_id=node_id)
    if st is None or len(st) == 0:
        return

    st = np.array(st)
    if np.any(st <= 0.0):
        io.log_exception('spike train {} contains negative/zero time, unable to run virtual cell in NEST'.format(st))
    st.sort()
    nest.SetStatus(nest_obj, {'spike_times': st})


if nest_version[0] >= 3:
    set_spikes = set_spikes_nest3
else:
    set_spikes = set_spikes_nest2


class PointNetwork(SimNetwork):
    def __init__(self, **properties):
        super(PointNetwork, self).__init__(**properties)
        self._io = io

        self.__weight_functions = {}
        self._params_cache = {}

        self._virtual_ids_map = {}
        self._batch_nodes = True

        # self._nest_id_map = {}
        self._nestid2nodeid_map = {}

        self._nestid2gid = {}

        self._nodes_table = {}
        self._gid2nestid = {}

        self._gid_map = GidPool()
        self._virtual_gids = GidPool()

    @property
    def py_function_caches(self):
        return pyfunction_cache

    @property
    def gid_map(self):
        return self._gid_map

    @property
    def gid_pool(self):
        return self._gid_map

    def get_nodes_df(self, population):
        nodes_adaptor = self.get_node_population(population)
        return nodes_adaptor.nodes_df()

    def __get_params(self, node_params):
        if node_params.with_dynamics_params:
            # TODO: use property, not name
            return node_params['dynamics_params']

        params_file = node_params[self._params_column]
        # params_file = self._MT.params_column(node_params) #node_params['dynamics_params']
        if params_file in self._params_cache:
            return self._params_cache[params_file]
        else:
            params_dir = self.get_component('models_dir')
            params_path = os.path.join(params_dir, params_file)
            params_dict = json.load(open(params_path, 'r'))
            self._params_cache[params_file] = params_dict
            return params_dict

    def _register_adaptors(self):
        super(PointNetwork, self)._register_adaptors()
        self._node_adaptors['sonata'] = PointNodeAdaptor
        self._edge_adaptors['sonata'] = PointEdgeAdaptor

    # TODO: reimplement with py_modules like in bionet
    def add_weight_function(self, fnc, name=None, **kwargs):
        fnc_name = name if name is not None else function.__name__
        self.__weight_functions[fnc_name] = functools.partial(fnc)

    def set_default_weight_function(self, fnc):
        self.add_weight_function(fnc, 'default_weight_fnc', overwrite=True)

    def get_weight_function(self, name):
        return self.__weight_functions[name]

    def get_node_id(self, population, node_id):
        pop = self.get_node_population(population)
        return pop.get_node(node_id)

    def build_nodes(self):
        for node_pop in self.node_populations:
            pop_name = node_pop.name
            gid_map = self.gid_map

            gid_map.create_pool(pop_name)
            if node_pop.internal_nodes_only:
                for node in node_pop.get_nodes():
                    node.build()
                    gid_map.add_nestids(name=pop_name, node_ids=node.node_ids, nest_ids=node.nest_ids)

            elif node_pop.mixed_nodes:
                for node in node_pop.get_nodes():
                    if node.model_type != 'virtual':
                        node.build()
                        gid_map.add_nestids(name=pop_name, node_ids=node.node_ids, nest_ids=node.nest_ids)

    def build_recurrent_edges(self, force_resolution=False):
        recurrent_edge_pops = [ep for ep in self._edge_populations if not ep.virtual_connections]
        if not recurrent_edge_pops:
            return

        for edge_pop in recurrent_edge_pops:
            for edge in edge_pop.get_edges():
                nest_srcs = self.gid_map.get_nestids(edge_pop.source_nodes, edge.source_node_ids)
                nest_trgs = self.gid_map.get_nestids(edge_pop.target_nodes, edge.target_node_ids)
                if isinstance(edge.nest_params['weight'], int):
                    edge.nest_params['weight'] = np.full(shape=len(nest_srcs),
                                                         fill_value=edge.nest_params['weight'])
                self._nest_connect(nest_srcs, nest_trgs, conn_spec='one_to_one', syn_spec=edge.nest_params)

    def find_edges(self, source_nodes=None, target_nodes=None):
        # TODO: Move to parent
        selected_edges = self._edge_populations[:]

        if source_nodes is not None:
            selected_edges = [edge_pop for edge_pop in selected_edges if edge_pop.source_nodes == source_nodes]

        if target_nodes is not None:
            selected_edges = [edge_pop for edge_pop in selected_edges if edge_pop.target_nodes == target_nodes]

        return selected_edges

    def add_spike_trains(self, spike_trains, node_set, sg_params={'precise_times': True}):
        # Build the virtual nodes
        src_nodes = [node_pop for node_pop in self.node_populations if node_pop.name in node_set.population_names()]
        virt_gid_map = self._virtual_gids
        for node_pop in src_nodes:
            if node_pop.name in self._virtual_ids_map:
                continue

            virt_node_map = {}
            if node_pop.virtual_nodes_only:
                for node in node_pop.get_nodes():
                    nest_objs = nest.Create('spike_generator', node.n_nodes, sg_params)
                    nest_ids = nest_objs.tolist() if nest_version[0] >= 3 else nest_objs

                    virt_gid_map.add_nestids(name=node_pop.name, nest_ids=nest_ids, node_ids=node.node_ids)
                    for node_id, nest_obj, nest_id in zip(node.node_ids, nest_objs, nest_ids):
                        virt_node_map[node_id] = nest_id
                        set_spikes(node_id=node_id, nest_obj=nest_obj, spike_trains=spike_trains)

            elif node_pop.mixed_nodes:
                for node in node_pop.get_nodes():
                    if node.model_type != 'virtual':
                        continue

                    nest_ids = nest.Create('spike_generator', node.n_nodes, sg_params)
                    for node_id, nest_id in zip(node.node_ids, nest_ids):
                        virt_node_map[node_id] = nest_id
                        set_spikes(node_id=node_id, nest_id=nest_id, spike_trains=spike_trains)

            self._virtual_ids_map[node_pop.name] = virt_node_map

        # Create virtual synaptic connections
        for source_reader in src_nodes:
            for edge_pop in self.find_edges(source_nodes=source_reader.name):
                for edge in edge_pop.get_edges():
                    nest_trgs = self.gid_map.get_nestids(edge_pop.target_nodes, edge.target_node_ids)
                    nest_srcs = virt_gid_map.get_nestids(edge_pop.source_nodes, edge.source_node_ids)
                    if isinstance(edge.nest_params['weight'], int):
                        edge.nest_params['weight'] = np.full(shape=len(nest_srcs),
                                                             fill_value=edge.nest_params['weight'])
                    self._nest_connect(nest_srcs, nest_trgs, conn_spec='one_to_one', syn_spec=edge.nest_params)

    def _nest_connect(self, nest_srcs, nest_trgs, conn_spec='one_to_one', syn_spec=None):
        """Calls nest.Connect but with some extra error logging and exception handling."""
        try:
            nest.Connect(nest_srcs, nest_trgs, conn_spec=conn_spec, syn_spec=syn_spec)

        except nest.kernel.NESTErrors.BadDelay as bde:
            # An occuring issue is when dt > delay, add some extra messaging in log to help users fix problem.
            res_kernel = nest.GetKernelStatus().get('resolution', 'NaN')
            delay_edges = syn_spec.get('delay', 'NaN')
            msg = 'synaptic "delay" value in edges ({}) is not compatible with simulator resolution/"dt" ({})'.format(
                delay_edges, res_kernel
            )
            self.io.log_error('{}{}'.format(bde.errorname, bde.errormessage))
            self.io.log_error(msg)
            raise

        except Exception as e:
            # Record exception to log file.
            self.io.log_error(str(e))
            raise
