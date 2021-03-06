# -*- coding: utf-8 -*-

"""
Nodenet definition
"""
import json
import os
import copy
import warnings

import theano
from theano import tensor as T
import numpy as np
import scipy.sparse as sp
import scipy

from micropsi_core.nodenet import monitor
from micropsi_core.nodenet.nodenet import Nodenet
from micropsi_core.nodenet.node import Nodetype

from micropsi_core.nodenet.theano_engine import theano_node as tnode
from micropsi_core.nodenet.theano_engine import theano_nodespace as tnodespace
from micropsi_core.nodenet.theano_engine.theano_node import *
from micropsi_core.nodenet.theano_engine.theano_stepoperators import *
from micropsi_core.nodenet.theano_engine.theano_nodespace import *
from micropsi_core.nodenet.theano_engine.theano_netapi import TheanoNetAPI

from configuration import config as settings


STANDARD_NODETYPES = {
    "Nodespace": {
        "name": "Nodespace"
    },
    "Register": {
        "name": "Register",
        "slottypes": ["gen"],
        "nodefunction_name": "register",
        "gatetypes": ["gen"]
    },
    "Sensor": {
        "name": "Sensor",
        "parameters": ["datasource"],
        "nodefunction_name": "sensor",
        "gatetypes": ["gen"]
    },
    "Actor": {
        "name": "Actor",
        "parameters": ["datatarget"],
        "nodefunction_name": "actor",
        "slottypes": ["gen"],
        "gatetypes": ["gen"]
    },
    "Pipe": {
        "name": "Pipe",
        "slottypes": ["gen", "por", "ret", "sub", "sur", "cat", "exp"],
        "nodefunction_name": "pipe",
        "gatetypes": ["gen", "por", "ret", "sub", "sur", "cat", "exp"],
        "gate_defaults": {
            "gen": {
                "minimum": -1,
                "maximum": 1,
                "threshold": -1,
                "spreadsheaves": 0
            },
            "por": {
                "minimum": -1,
                "maximum": 1,
                "threshold": -1,
                "spreadsheaves": 0
            },
            "ret": {
                "minimum": -1,
                "maximum": 1,
                "threshold": -1,
                "spreadsheaves": 0
            },
            "sub": {
                "minimum": -1,
                "maximum": 1,
                "threshold": -1,
                "spreadsheaves": True
            },
            "sur": {
                "minimum": -1,
                "maximum": 1,
                "threshold": -1,
                "spreadsheaves": 0
            },
            "cat": {
                "minimum": -1,
                "maximum": 1,
                "threshold": -1,
                "spreadsheaves": 1
            },
            "exp": {
                "minimum": -1,
                "maximum": 1,
                "threshold": -1,
                "spreadsheaves": 0
            }
        },
        'symbol': 'πp'
    },
    "Activator": {
        "name": "Activator",
        "slottypes": ["gen"],
        "parameters": ["type"],
        "parameter_values": {"type": ["por", "ret", "sub", "sur", "cat", "exp"]},
        "nodefunction_name": "activator"
    }
}

NODENET_VERSION = 1

AVERAGE_ELEMENTS_PER_NODE_ASSUMPTION = 3

DEFAULT_NUMBER_OF_NODES = 2000
DEFAULT_NUMBER_OF_ELEMENTS = DEFAULT_NUMBER_OF_NODES * AVERAGE_ELEMENTS_PER_NODE_ASSUMPTION
DEFAULT_NUMBER_OF_NODESPACES = 100

class TheanoNodenet(Nodenet):
    """
        theano runtime engine implementation
    """

    # array, index is node id, value is numeric node type
    allocated_nodes = None

    # array, index is node id, value is offset in a and w
    allocated_node_offsets = None

    # array, index is node id, value is nodespace id
    allocated_node_parents = None

    # array, index is element index, value is node id
    allocated_elements_to_nodes = None

    # array, index is nodespace id, value is parent nodespace id
    allocated_nodespaces = None

    # directional activator assignment, key is nodespace ID, value is activator ID
    allocated_nodespaces_por_activators = None
    allocated_nodespaces_ret_activators = None
    allocated_nodespaces_sub_activators = None
    allocated_nodespaces_sur_activators = None
    allocated_nodespaces_cat_activators = None
    allocated_nodespaces_exp_activators = None

    # directional activators map, index is element id, value is the directional activator's element id
    allocated_elements_to_activators = None

    last_allocated_node = 0
    last_allocated_offset = 0
    last_allocated_nodespace = 0

    native_module_instances = {}

    # todo: get rid of positions
    # map of string uids to positions. Not all nodes necessarily have an entry.
    positions = {}

    # map of string uids to names. Not all nodes neccessarily have an entry.
    names = {}

    # map of data sources to numerical node IDs
    sensormap = {}

    # map of numerical node IDs to data sources
    inverted_sensor_map = {}

    # map of data targets to numerical node IDs
    actuatormap = {}

    # map of numerical node IDs to data targets
    inverted_actuator_map = {}

    # theano tensors for performing operations
    w = None            # matrix of weights
    a = None            # vector of activations
    a_shifted = None    # matrix with each row defined as [a[n], a[n+1], a[n+2], a[n+3], a[n+4], a[n+5], a[n+6]]
                        # this is a view on the activation values instrumental in calculating concept node functions

    g_factor = None     # vector of gate factors, controlled by directional activators
    g_threshold = None  # vector of thresholds (gate parameters)
    g_amplification = None  # vector of amplification factors
    g_min = None        # vector of lower bounds
    g_max = None        # vector of upper bounds

    g_function_selector = None # vector of gate function selectors

    g_theta = None      # vector of thetas (i.e. biases, use depending on gate function)

    n_function_selector = None      # vector of per-gate node function selectors
    n_node_porlinked = None         # vector with 0/1 flags to indicated whether the element belongs to a por-linked
                                    # node. This could in theory be inferred with T.max() on upshifted versions of w,
                                    # but for now, we manually track this property
    n_node_retlinked = None         # same for ret

    sparse = True

    __has_new_usages = True
    __has_pipes = False
    __has_directional_activators = False
    __has_gatefunction_absolute = False
    __has_gatefunction_sigmoid = False
    __has_gatefunction_tanh = False
    __has_gatefunction_rect = False
    __has_gatefunction_one_over_x = False

    @property
    def engine(self):
        return "theano_engine"

    @property
    def current_step(self):
        return self.__step

    @property
    def data(self):
        data = super(TheanoNodenet, self).data
        data['links'] = self.construct_links_dict()
        data['nodes'] = self.construct_nodes_dict()
        # for uid in data['nodes']:
        #    data['nodes'][uid]['gate_parameters'] = self.get_node(uid).clone_non_default_gate_parameters()
        data['nodespaces'] = self.construct_nodespaces_dict(None)
        data['version'] = self.__version
        data['modulators'] = self.construct_modulators_dict()
        return data

    @property
    def has_new_usages(self):
        return self.__has_new_usages

    @has_new_usages.setter
    def has_new_usages(self, value):
        self.__has_new_usages = value

    @property
    def has_pipes(self):
        return self.__has_pipes

    @has_pipes.setter
    def has_pipes(self, value):
        if value != self.__has_pipes:
            self.__has_new_usages = True
            self.__has_pipes = value

    @property
    def has_directional_activators(self):
        return self.__has_directional_activators

    @has_directional_activators.setter
    def has_directional_activators(self, value):
        if value != self.__has_directional_activators:
            self.__has_new_usages = True
            self.__has_directional_activators = value

    @property
    def has_gatefunction_absolute(self):
        return self.__has_gatefunction_absolute

    @has_gatefunction_absolute.setter
    def has_gatefunction_absolute(self, value):
        if value != self.__has_gatefunction_absolute:
            self.__has_new_usages = True
            self.__has_gatefunction_absolute = value

    @property
    def has_gatefunction_sigmoid(self):
        return self.__has_gatefunction_sigmoid

    @has_gatefunction_sigmoid.setter
    def has_gatefunction_sigmoid(self, value):
        if value != self.__has_gatefunction_sigmoid:
            self.__has_new_usages = True
            self.__has_gatefunction_sigmoid = value

    @property
    def has_gatefunction_tanh(self):
        return self.__has_gatefunction_tanh

    @has_gatefunction_tanh.setter
    def has_gatefunction_tanh(self, value):
        if value != self.__has_gatefunction_tanh:
            self.__has_new_usages = True
            self.__has_gatefunction_tanh = value

    @property
    def has_gatefunction_rect(self):
        return self.__has_gatefunction_rect

    @has_gatefunction_rect.setter
    def has_gatefunction_rect(self, value):
        if value != self.__has_gatefunction_rect:
            self.__has_new_usages = True
            self.__has_gatefunction_rect = value

    @property
    def has_gatefunction_one_over_x(self):
        return self.__has_gatefunction_one_over_x

    @has_gatefunction_one_over_x.setter
    def has_gatefunction_one_over_x(self, value):
        if value != self.__has_gatefunction_one_over_x:
            self.__has_new_usages = True
            self.__has_gatefunction_one_over_x = value

    def __init__(self, name="", worldadapter="Default", world=None, owner="", uid=None, native_modules={}):

        super(TheanoNodenet, self).__init__(name, worldadapter, world, owner, uid)

        self.sparse = True
        configuredsparse = settings['theano']['sparse_weight_matrix']
        if configuredsparse == "True":
            self.sparse = True
        elif configuredsparse == "False":
            self.sparse = False
        else:
            self.logger.warn("Unsupported sparse_weight_matrix value from configuration: %s, falling back to True", configuredsparse)
            self.sparse = True

        precision = settings['theano']['precision']
        if precision == "32":
            T.config.floatX = "float32"
            scipyfloatX = scipy.float32
            numpyfloatX = np.float32
            self.byte_per_float = 4
        elif precision == "64":
            T.config.floatX = "float64"
            scipyfloatX = scipy.float64
            numpyfloatX = np.float64
            self.byte_per_float = 8
        else:
            self.logger.warn("Unsupported precision value from configuration: %s, falling back to float64", precision)
            T.config.floatX = "float64"
            scipyfloatX = scipy.float64
            numpyfloatX = np.float64
            self.byte_per_float = 8

        device = T.config.device
        self.logger.info("Theano configured to use %s", device)
        if device.startswith("gpu"):
            self.logger.info("Using CUDA with cuda_root=%s and theano_flags=%s", os.environ["CUDA_ROOT"], os.environ["THEANO_FLAGS"])
            if T.config.floatX != "float32":
                self.logger.warn("Precision set to %s, but attempting to use gpu.", precision)

        self.netapi = TheanoNetAPI(self)

        self.NoN = DEFAULT_NUMBER_OF_NODES
        self.NoE = DEFAULT_NUMBER_OF_ELEMENTS
        self.NoNS = DEFAULT_NUMBER_OF_NODESPACES

        self.__version = NODENET_VERSION  # used to check compatibility of the node net data
        self.__step = 0
        self.__modulators = {}

        self.allocated_nodes = np.zeros(self.NoN, dtype=np.int32)
        self.allocated_node_parents = np.zeros(self.NoN, dtype=np.int32)
        self.allocated_node_offsets = np.zeros(self.NoN, dtype=np.int32)
        self.allocated_elements_to_nodes = np.zeros(self.NoE, dtype=np.int32)
        self.allocated_elements_to_activators = np.zeros(self.NoE, dtype=np.int32)
        self.allocated_nodespaces = np.zeros(self.NoNS, dtype=np.int32)

        self.allocated_nodespaces_por_activators = np.zeros(self.NoNS, dtype=np.int32)
        self.allocated_nodespaces_ret_activators = np.zeros(self.NoNS, dtype=np.int32)
        self.allocated_nodespaces_sub_activators = np.zeros(self.NoNS, dtype=np.int32)
        self.allocated_nodespaces_sur_activators = np.zeros(self.NoNS, dtype=np.int32)
        self.allocated_nodespaces_cat_activators = np.zeros(self.NoNS, dtype=np.int32)
        self.allocated_nodespaces_exp_activators = np.zeros(self.NoNS, dtype=np.int32)

        if self.sparse:
            self.w = theano.shared(sp.csr_matrix((self.NoE, self.NoE), dtype=scipyfloatX), name="w")
        else:
            w_matrix = np.zeros((self.NoE, self.NoE), dtype=scipyfloatX)
            self.w = theano.shared(value=w_matrix.astype(T.config.floatX), name="w", borrow=True)

        a_array = np.zeros(self.NoE, dtype=numpyfloatX)
        self.a = theano.shared(value=a_array.astype(T.config.floatX), name="a", borrow=True)

        a_shifted_matrix = np.lib.stride_tricks.as_strided(a_array, shape=(self.NoE, 7), strides=(self.byte_per_float, self.byte_per_float))
        self.a_shifted = theano.shared(value=a_shifted_matrix.astype(T.config.floatX), name="a_shifted", borrow=True)

        g_theta_array = np.zeros(self.NoE, dtype=numpyfloatX)
        self.g_theta = theano.shared(value=g_theta_array.astype(T.config.floatX), name="theta", borrow=True)

        g_factor_array = np.ones(self.NoE, dtype=numpyfloatX)
        self.g_factor = theano.shared(value=g_factor_array.astype(T.config.floatX), name="g_factor", borrow=True)

        g_threshold_array = np.zeros(self.NoE, dtype=numpyfloatX)
        self.g_threshold = theano.shared(value=g_threshold_array.astype(T.config.floatX), name="g_threshold", borrow=True)

        g_amplification_array = np.ones(self.NoE, dtype=numpyfloatX)
        self.g_amplification = theano.shared(value=g_amplification_array.astype(T.config.floatX), name="g_amplification", borrow=True)

        g_min_array = np.zeros(self.NoE, dtype=numpyfloatX)
        self.g_min = theano.shared(value=g_min_array.astype(T.config.floatX), name="g_min", borrow=True)

        g_max_array = np.ones(self.NoE, dtype=numpyfloatX)
        self.g_max = theano.shared(value=g_max_array.astype(T.config.floatX), name="g_max", borrow=True)

        g_function_selector_array = np.zeros(self.NoE, dtype=np.int8)
        self.g_function_selector = theano.shared(value=g_function_selector_array, name="gatefunction", borrow=True)

        n_function_selector_array = np.zeros(self.NoE, dtype=np.int8)
        self.n_function_selector = theano.shared(value=n_function_selector_array, name="nodefunction_per_gate", borrow=True)

        n_node_porlinked_array = np.zeros(self.NoE, dtype=np.int8)
        self.n_node_porlinked = theano.shared(value=n_node_porlinked_array, name="porlinked", borrow=True)

        n_node_retlinked_array = np.zeros(self.NoE, dtype=np.int8)
        self.n_node_retlinked = theano.shared(value=n_node_retlinked_array, name="retlinked", borrow=True)

        self.initialize_stepoperators()

        self.__nodetypes = {}
        for type, data in STANDARD_NODETYPES.items():
            self.__nodetypes[type] = Nodetype(nodenet=self, **data)

        self.native_modules = {}
        for type, data in native_modules.items():
            self.native_modules[type] = Nodetype(nodenet=self, **data)

        self.nodegroups = {}

        self.create_nodespace(None, None, "Root", tnodespace.to_id(1))

        self.initialize_nodenet({})

    def initialize_stepoperators(self):
        self.stepoperators = [TheanoPropagate(self), TheanoCalculate(self)]
        self.stepoperators.sort(key=lambda op: op.priority)

    def save(self, filename):

        # write json metadata, which will be used by runtime to manage the net
        with open(filename, 'w+') as fp:
            metadata = self.metadata
            metadata['positions'] = self.positions
            metadata['names'] = self.names
            metadata['actuatormap'] = self.actuatormap
            metadata['sensormap'] = self.sensormap
            fp.write(json.dumps(metadata, sort_keys=True, indent=4))

        # write bulk data to our own numpy-based file format
        datafilename = os.path.join(os.path.dirname(filename), self.uid + "-data")

        allocated_nodes = self.allocated_nodes
        allocated_node_offsets = self.allocated_node_offsets
        allocated_elements_to_nodes = self.allocated_elements_to_nodes
        allocated_node_parents = self.allocated_node_parents
        allocated_nodespaces = self.allocated_nodespaces
        allocated_elements_to_activators = self.allocated_elements_to_activators

        allocated_nodespaces_por_activators = self.allocated_nodespaces_por_activators
        allocated_nodespaces_ret_activators = self.allocated_nodespaces_ret_activators
        allocated_nodespaces_sub_activators = self.allocated_nodespaces_sub_activators
        allocated_nodespaces_sur_activators = self.allocated_nodespaces_sur_activators
        allocated_nodespaces_cat_activators = self.allocated_nodespaces_cat_activators
        allocated_nodespaces_exp_activators = self.allocated_nodespaces_exp_activators


        w = self.w.get_value(borrow=True)

        # if we're sparse, convert to sparse matrix for persistency
        if not self.sparse:
            w = sp.csr_matrix(w)

        a = self.a.get_value(borrow=True)
        g_theta = self.g_theta.get_value(borrow=True)
        g_factor = self.g_factor.get_value(borrow=True)
        g_threshold = self.g_threshold.get_value(borrow=True)
        g_amplification = self.g_amplification.get_value(borrow=True)
        g_min = self.g_min.get_value(borrow=True)
        g_max = self.g_max.get_value(borrow=True)
        g_function_selector = self.g_function_selector.get_value(borrow=True)
        n_function_selector = self.n_function_selector.get_value(borrow=True)
        n_node_porlinked = self.n_node_porlinked.get_value(borrow=True)
        n_node_retlinked = self.n_node_retlinked.get_value(borrow=True)

        sizeinformation = [self.NoN, self.NoE, self.NoNS]

        np.savez(datafilename,
                 allocated_nodes=allocated_nodes,
                 allocated_node_offsets=allocated_node_offsets,
                 allocated_elements_to_nodes=allocated_elements_to_nodes,
                 allocated_node_parents=allocated_node_parents,
                 allocated_nodespaces=allocated_nodespaces,
                 w_data=w.data,
                 w_indices=w.indices,
                 w_indptr=w.indptr,
                 a=a,
                 g_theta=g_theta,
                 g_factor=g_factor,
                 g_threshold=g_threshold,
                 g_amplification=g_amplification,
                 g_min=g_min,
                 g_max=g_max,
                 g_function_selector=g_function_selector,
                 n_function_selector=n_function_selector,
                 n_node_porlinked=n_node_porlinked,
                 n_node_retlinked=n_node_retlinked,
                 sizeinformation=sizeinformation,
                 allocated_elements_to_activators=allocated_elements_to_activators,
                 allocated_nodespaces_por_activators=allocated_nodespaces_por_activators,
                 allocated_nodespaces_ret_activators=allocated_nodespaces_ret_activators,
                 allocated_nodespaces_sub_activators=allocated_nodespaces_sub_activators,
                 allocated_nodespaces_sur_activators=allocated_nodespaces_sur_activators,
                 allocated_nodespaces_cat_activators=allocated_nodespaces_cat_activators,
                 allocated_nodespaces_exp_activators=allocated_nodespaces_exp_activators)

    def load(self, filename):
        """Load the node net from a file"""
        # try to access file

        datafilename = os.path.join(os.path.dirname(filename), self.uid + "-data.npz")

        with self.netlock:
            initfrom = {}
            datafile = None
            if os.path.isfile(filename):
                try:
                    self.logger.info("Loading nodenet %s metadata from file %s", self.name, filename)
                    with open(filename) as file:
                        initfrom.update(json.load(file))
                except ValueError:
                    warnings.warn("Could not read nodenet metadata from file %s", filename)
                    return False
                except IOError:
                    warnings.warn("Could not open nodenet metadata file %s", filename)
                    return False

            if os.path.isfile(datafilename):
                try:
                    self.logger.info("Loading nodenet %s bulk data from file %s", self.name, datafilename)
                    datafile = np.load(datafilename)
                except ValueError:
                    warnings.warn("Could not read nodenet data from file %", datafile)
                    return False
                except IOError:
                    warnings.warn("Could not open nodenet file %s", datafile)
                    return False

            # initialize with metadata
            self.initialize_nodenet(initfrom)

            if datafile:

                if 'sizeinformation' in datafile:
                    self.NoN = datafile['sizeinformation'][0]
                    self.NoE = datafile['sizeinformation'][1]
                else:
                    self.logger.warn("no sizeinformation in file, falling back to defaults")

                # the load bulk data into numpy arrays
                if 'allocated_nodes' in datafile:
                    self.allocated_nodes = datafile['allocated_nodes']
                else:
                    self.logger.warn("no allocated_nodes in file, falling back to defaults")

                if 'allocated_node_offsets' in datafile:
                    self.allocated_node_offsets = datafile['allocated_node_offsets']
                else:
                    self.logger.warn("no allocated_node_offsets in file, falling back to defaults")

                if 'allocated_elements_to_nodes' in datafile:
                    self.allocated_elements_to_nodes = datafile['allocated_elements_to_nodes']
                else:
                    self.logger.warn("no allocated_elements_to_nodes in file, falling back to defaults")

                if 'allocated_nodespaces' in datafile:
                    self.allocated_nodespaces = datafile['allocated_nodespaces']
                else:
                    self.logger.warn("no allocated_nodespaces in file, falling back to defaults")

                if 'allocated_node_parents' in datafile:
                    self.allocated_node_parents = datafile['allocated_node_parents']
                else:
                    self.logger.warn("no allocated_node_parents in file, falling back to defaults")

                if 'allocated_elements_to_activators' in datafile:
                    self.allocated_elements_to_activators = datafile['allocated_elements_to_activators']
                else:
                    self.logger.warn("no allocated_elements_to_activators in file, falling back to defaults")

                if 'allocated_nodespaces_por_activators' in datafile:
                    self.allocated_nodespaces_por_activators = datafile['allocated_nodespaces_por_activators']
                else:
                    self.logger.warn("no allocated_nodespaces_por_activators in file, falling back to defaults")

                if 'allocated_nodespaces_ret_activators' in datafile:
                    self.allocated_nodespaces_ret_activators = datafile['allocated_nodespaces_ret_activators']
                else:
                    self.logger.warn("no allocated_nodespaces_ret_activators in file, falling back to defaults")

                if 'allocated_nodespaces_sub_activators' in datafile:
                    self.allocated_nodespaces_sub_activators = datafile['allocated_nodespaces_sub_activators']
                else:
                    self.logger.warn("no allocated_nodespaces_sub_activators in file, falling back to defaults")

                if 'allocated_nodespaces_sur_activators' in datafile:
                    self.allocated_nodespaces_sur_activators = datafile['allocated_nodespaces_sur_activators']
                else:
                    self.logger.warn("no allocated_nodespaces_sur_activators in file, falling back to defaults")

                if 'allocated_nodespaces_cat_activators' in datafile:
                    self.allocated_nodespaces_cat_activators = datafile['allocated_nodespaces_cat_activators']
                else:
                    self.logger.warn("no allocated_nodespaces_cat_activators in file, falling back to defaults")

                if 'allocated_nodespaces_exp_activators' in datafile:
                    self.allocated_nodespaces_exp_activators = datafile['allocated_nodespaces_exp_activators']
                else:
                    self.logger.warn("no allocated_nodespaces_exp_activators in file, falling back to defaults")


                if 'w_data' in datafile and 'w_indices' in datafile and 'w_indptr' in datafile:
                    w = sp.csr_matrix((datafile['w_data'], datafile['w_indices'], datafile['w_indptr']), shape = (self.NoE, self.NoE))
                    # if we're configured to be dense, convert from csr
                    if not self.sparse:
                        w = w.todense()
                    self.w = theano.shared(value=w.astype(T.config.floatX), name="w", borrow=False)
                    self.a = theano.shared(value=datafile['a'].astype(T.config.floatX), name="a", borrow=False)
                else:
                    self.logger.warn("no w_data, w_indices or w_indptr in file, falling back to defaults")

                if 'g_theta' in datafile:
                    self.g_theta = theano.shared(value=datafile['g_theta'].astype(T.config.floatX), name="theta", borrow=False)
                else:
                    self.logger.warn("no g_theta in file, falling back to defaults")

                if 'g_factor' in datafile:
                    self.g_factor = theano.shared(value=datafile['g_factor'].astype(T.config.floatX), name="g_factor", borrow=False)
                else:
                    self.logger.warn("no g_factor in file, falling back to defaults")

                if 'g_threshold' in datafile:
                    self.g_threshold = theano.shared(value=datafile['g_threshold'].astype(T.config.floatX), name="g_threshold", borrow=False)
                else:
                    self.logger.warn("no g_threshold in file, falling back to defaults")

                if 'g_amplification' in datafile:
                    self.g_amplification = theano.shared(value=datafile['g_amplification'].astype(T.config.floatX), name="g_amplification", borrow=False)
                else:
                    self.logger.warn("no g_amplification in file, falling back to defaults")

                if 'g_min' in datafile:
                    self.g_min = theano.shared(value=datafile['g_min'].astype(T.config.floatX), name="g_min", borrow=False)
                else:
                    self.logger.warn("no g_min in file, falling back to defaults")

                if 'g_max' in datafile:
                    self.g_max = theano.shared(value=datafile['g_max'].astype(T.config.floatX), name="g_max", borrow=False)
                else:
                    self.logger.warn("no g_max in file, falling back to defaults")

                if 'g_function_selector' in datafile:
                    self.g_function_selector = theano.shared(value=datafile['g_function_selector'], name="gatefunction", borrow=False)
                else:
                    self.logger.warn("no g_function_selector in file, falling back to defaults")

                if 'n_function_selector' in datafile:
                    self.n_function_selector = theano.shared(value=datafile['n_function_selector'], name="nodefunction_per_gate", borrow=False)
                else:
                    self.logger.warn("no n_function_selector in file, falling back to defaults")


                if 'n_node_porlinked' in datafile:
                    self.n_node_porlinked = theano.shared(value=datafile['n_node_porlinked'], name="porlinked", borrow=False)
                else:
                    self.logger.warn("no n_node_porlinked in file, falling back to defaults")

                if 'n_node_retlinked' in datafile:
                    self.n_node_retlinked = theano.shared(value=datafile['n_node_retlinked'], name="retlinked", borrow=False)
                else:
                    self.logger.warn("no n_node_retlinked in file, falling back to defaults")

                # reconstruct other states
                if 'g_function_selector' in datafile:
                    g_function_selector = datafile['g_function_selector']
                    self.has_new_usages = True
                    self.has_pipes = PIPE in self.allocated_nodes
                    self.has_directional_activators = ACTIVATOR in self.allocated_nodes
                    self.has_gatefunction_absolute = GATE_FUNCTION_ABSOLUTE in g_function_selector
                    self.has_gatefunction_sigmoid = GATE_FUNCTION_SIGMOID in g_function_selector
                    self.has_gatefunction_tanh = GATE_FUNCTION_TANH in g_function_selector
                    self.has_gatefunction_rect = GATE_FUNCTION_RECT in g_function_selector
                    self.has_gatefunction_one_over_x = GATE_FUNCTION_DIST in g_function_selector
                else:
                    self.logger.warn("no g_function_selector in file, falling back to defaults")

                for id in range(len(self.allocated_nodes)):
                    if self.allocated_nodes[id] > MAX_STD_NODETYPE:
                        uid = tnode.to_id(id)
                        self.native_module_instances[uid] = self.get_node(uid)

            for sensor, id_list in self.sensormap.items():
                for id in id_list:
                    self.inverted_sensor_map[tnode.to_id(id)] = sensor
            for actuator, id_list in self.actuatormap.items():
                for id in id_list:
                    self.inverted_actuator_map[tnode.to_id(id)] = actuator

            # re-initialize step operators for theano recompile to new shared variables
            self.initialize_stepoperators()

            return True

    def remove(self, filename):
        datafilename = os.path.join(os.path.dirname(filename), self.uid + "-data.npz")
        os.remove(datafilename)
        os.remove(filename)

    def initialize_nodenet(self, initfrom):

        # todo: implement modulators
        # self.__modulators = initfrom.get("modulators", {})

        if len(initfrom) != 0:
            # now merge in all init data (from the persisted file typically)
            self.merge_data(initfrom, keep_uids=True)
            if 'names' in initfrom:
                self.names = initfrom['names']
            if 'positions' in initfrom:
                self.positions = initfrom['positions']
            if 'actuatormap' in initfrom:
                self.actuatormap = initfrom['actuatormap']
            if 'sensormap' in initfrom:
                self.sensormap = initfrom['sensormap']


    def merge_data(self, nodenet_data, keep_uids=False):
        """merges the nodenet state with the current node net, might have to give new UIDs to some entities"""

        uidmap = {}
        # for dict_engine compatibility
        uidmap["Root"] = "s1"

        # re-use the root nodespace
        uidmap["s1"] = "s1"

        # merge in spaces, make sure that parent nodespaces exist before children are initialized
        nodespaces_to_merge = set(nodenet_data.get('nodespaces', {}).keys())
        for nodespace in nodespaces_to_merge:
            self.merge_nodespace_data(nodespace, nodenet_data['nodespaces'], uidmap, keep_uids)

        # merge in nodes
        for uid in nodenet_data.get('nodes', {}):
            data = nodenet_data['nodes'][uid]
            parent_uid = data['parent_nodespace']
            if not keep_uids:
                parent_uid = uidmap[data['parent_nodespace']]
            if data['type'] in self.__nodetypes or data['type'] in self.native_modules:
                olduid = None
                if keep_uids:
                    olduid = uid
                new_uid = self.create_node(
                    data['type'],
                    parent_uid,
                    data['position'],
                    name=data['name'],
                    uid=olduid,
                    parameters=data['parameters'],
                    gate_parameters=data['gate_parameters'],
                    gate_functions=data['gate_functions'])
                uidmap[uid] = new_uid
                node_proxy = self.get_node(new_uid)
                for gatetype in data['gate_activations']:   # todo: implement sheaves
                    node_proxy.get_gate(gatetype).activation = data['gate_activations'][gatetype]['default']['activation']

            else:
                warnings.warn("Invalid nodetype %s for node %s" % (data['type'], uid))

        # merge in links
        for linkid in nodenet_data.get('links', {}):
            data = nodenet_data['links'][linkid]
            self.create_link(
                uidmap[data['source_node_uid']],
                data['source_gate_name'],
                uidmap[data['target_node_uid']],
                data['target_slot_name'],
                data['weight']
            )

        for monitorid in nodenet_data.get('monitors', {}):
            data = nodenet_data['monitors'][monitorid]
            if 'node_uid' in data:
                old_node_uid = data['node_uid']
                if old_node_uid in uidmap:
                    data['node_uid'] = uidmap[old_node_uid]
            if 'classname' in data:
                if hasattr(monitor, data['classname']):
                    getattr(monitor, data['classname'])(self, **data)
                else:
                    self.logger.warn('unknown classname for monitor: %s (uid:%s) ' % (data['classname'], monitorid))
            else:
                # Compatibility mode
                monitor.NodeMonitor(self, name=data['node_name'], **data)

    def merge_nodespace_data(self, nodespace_uid, data, uidmap, keep_uids=False):
        """
        merges the given nodespace with the given nodespace data dict
        This will make sure all parent nodespaces for the given nodespace exist (and create the parents
        if necessary)
        """
        if keep_uids:
            id = tnodespace.from_id(nodespace_uid)
            if self.allocated_nodespaces[id] == 0:
                # move up the nodespace tree until we find an existing parent or hit root
                if id != 1:
                    parent_id = tnodespace.from_id(data[nodespace_uid].get('parent_nodespace'))
                    if self.allocated_nodespaces[parent_id] == 0:
                        self.merge_nodespace_data(tnodespace.to_id(parent_id), data, uidmap, keep_uids)
                self.create_nodespace(
                    data[nodespace_uid].get('parent_nodespace'),
                    data[nodespace_uid].get('position'),
                    name=data[nodespace_uid].get('name', 'Root'),
                    uid=nodespace_uid
                )
        else:
            if not nodespace_uid in uidmap:
                parent_uid = data[nodespace_uid].get('parent_nodespace')
                if not parent_uid in uidmap:
                    self.merge_nodespace_data(parent_uid, data, uidmap, keep_uids)
                newuid = self.create_nodespace(
                    uidmap[data[nodespace_uid].get('parent_nodespace')],
                    data[nodespace_uid].get('position'),
                    name=data[nodespace_uid].get('name', 'Root'),
                    uid=None
                )
                uidmap[nodespace_uid] = newuid

    def step(self):
        self.user_prompt = None
        if self.world is not None and self.world.agents is not None and self.uid in self.world.agents:
            self.world.agents[self.uid].snapshot()      # world adapter snapshot
                                                        # TODO: Not really sure why we don't just know our world adapter,
                                                        # but instead the world object itself

        with self.netlock:

            # self.timeout_locks()

            for operator in self.stepoperators:
                operator.execute(self, None, self.netapi)

            self.netapi._step()

            self.__step += 1

    def get_node(self, uid):
        if uid in self.native_module_instances:
            return self.native_module_instances[uid]
        elif uid in self.get_node_uids():
            id = tnode.from_id(uid)
            parent_id = self.allocated_node_parents[id]
            return TheanoNode(self, tnodespace.to_id(parent_id), uid, self.allocated_nodes[id])
        else:
            return None

    def get_node_uids(self):
        return [tnode.to_id(id) for id in np.nonzero(self.allocated_nodes)[0]]

    def is_node(self, uid):
        return uid in self.get_node_uids()

    def create_node(self, nodetype, nodespace_uid, position, name=None, uid=None, parameters=None, gate_parameters=None, gate_functions=None):

        # find a free ID / index in the allocated_nodes vector to hold the node type
        if uid is None:
            id = 0
            for i in range((self.last_allocated_node + 1), self.NoN):
                if self.allocated_nodes[i] == 0:
                    id = i
                    break

            if id < 1:
                for i in range(self.last_allocated_node - 1):
                    if self.allocated_nodes[i] == 0:
                        id = i
                        break

            if id < 1:
                raise MemoryError("Cannot find free id, all " + str(self.NoN) + " node entries already in use.")
        else:
            id = tnode.from_id(uid)

        uid = tnode.to_id(id)

        # now find a range of free elements to be used by this node
        number_of_elements = get_elements_per_type(get_numerical_node_type(nodetype, self.native_modules), self.native_modules)
        has_restarted_from_zero = False
        offset = 0
        i = self.last_allocated_offset + 1
        while offset < 1:
            freecount = 0
            for j in range(0, number_of_elements):
                if i+j < len(self.allocated_elements_to_nodes) and self.allocated_elements_to_nodes[i+j] == 0:
                    freecount += 1
                else:
                    break
            if freecount >= number_of_elements:
                offset = i
                break
            else:
                i += freecount+1

            if i >= self.NoE:
                if not has_restarted_from_zero:
                    i = 0
                    has_restarted_from_zero = True
                else:
                    raise MemoryError("Cannot find "+str(number_of_elements)+" consecutive free elements for new node " + uid)

        self.last_allocated_node = id
        self.last_allocated_offset = offset
        self.allocated_nodes[id] = get_numerical_node_type(nodetype, self.native_modules)
        self.allocated_node_parents[id] = tnodespace.from_id(nodespace_uid)
        self.allocated_node_offsets[id] = offset

        for element in range (0, get_elements_per_type(self.allocated_nodes[id], self.native_modules)):
            self.allocated_elements_to_nodes[offset + element] = id

        if position is not None:
            self.positions[uid] = position
        if name is not None and name != "" and name != uid:
            self.names[uid] = name

        if parameters is None:
            parameters = {}

        if nodetype == "Sensor":
            if 'datasource' in parameters:
                datasource = parameters['datasource']
                if datasource is not None:
                    connectedsensors = self.sensormap.get(datasource, [])
                    connectedsensors.append(id)
                    self.sensormap[datasource] = connectedsensors
                    self.inverted_sensor_map[uid] = datasource
        elif nodetype == "Actor":
            if 'datatarget' in parameters:
                datatarget = parameters['datatarget']
                if datatarget is not None:
                    connectedactuators = self.actuatormap.get(datatarget, [])
                    connectedactuators.append(id)
                    self.actuatormap[datatarget] = connectedactuators
                    self.inverted_actuator_map[uid] = datatarget
        elif nodetype == "Pipe":
            self.has_pipes = True
            n_function_selector_array = self.n_function_selector.get_value(borrow=True, return_internal_type=True)
            n_function_selector_array[offset + GEN] = NFPG_PIPE_GEN
            n_function_selector_array[offset + POR] = NFPG_PIPE_POR
            n_function_selector_array[offset + RET] = NFPG_PIPE_RET
            n_function_selector_array[offset + SUB] = NFPG_PIPE_SUB
            n_function_selector_array[offset + SUR] = NFPG_PIPE_SUR
            n_function_selector_array[offset + CAT] = NFPG_PIPE_CAT
            n_function_selector_array[offset + EXP] = NFPG_PIPE_EXP
            self.n_function_selector.set_value(n_function_selector_array, borrow=True)
            self.allocated_elements_to_activators[offset + POR] = \
                self.allocated_node_offsets[self.allocated_nodespaces_por_activators[tnodespace.from_id(nodespace_uid)]]
            self.allocated_elements_to_activators[offset + RET] = \
                self.allocated_node_offsets[self.allocated_nodespaces_ret_activators[tnodespace.from_id(nodespace_uid)]]
            self.allocated_elements_to_activators[offset + SUB] = \
                self.allocated_node_offsets[self.allocated_nodespaces_sub_activators[tnodespace.from_id(nodespace_uid)]]
            self.allocated_elements_to_activators[offset + SUR] = \
                self.allocated_node_offsets[self.allocated_nodespaces_sur_activators[tnodespace.from_id(nodespace_uid)]]
            self.allocated_elements_to_activators[offset + CAT] = \
                self.allocated_node_offsets[self.allocated_nodespaces_cat_activators[tnodespace.from_id(nodespace_uid)]]
            self.allocated_elements_to_activators[offset + EXP] = \
                self.allocated_node_offsets[self.allocated_nodespaces_exp_activators[tnodespace.from_id(nodespace_uid)]]
        elif nodetype == "Activator":
            self.has_directional_activators = True
            activator_type = parameters.get("type")
            if activator_type is not None and len(activator_type) > 0:
                self.set_nodespace_gatetype_activator(nodespace_uid, activator_type, uid)

        node_proxy = self.get_node(uid)
        for gate, parameters in self.get_nodetype(nodetype).gate_defaults.items():
            for gate_parameter in parameters:
                node_proxy.set_gate_parameter(gate, gate_parameter, parameters[gate_parameter])
        if gate_parameters is not None:
            for gate, parameters in gate_parameters.items():
                for gate_parameter in parameters:
                    node_proxy.set_gate_parameter(gate, gate_parameter, parameters[gate_parameter])

        if gate_functions is not None:
            for gate, gate_function in gate_functions.items():
                node_proxy.set_gatefunction_name(gate, gate_function)

        if nodetype not in STANDARD_NODETYPES:
            self.native_module_instances[uid] = node_proxy

        return uid

    def delete_node(self, uid):

        type = self.allocated_nodes[tnode.from_id(uid)]
        offset = self.allocated_node_offsets[tnode.from_id(uid)]
        parent = self.allocated_node_parents[tnode.from_id(uid)]

        # unlink
        self.get_node(uid).unlink_completely()

        # forget
        self.allocated_nodes[tnode.from_id(uid)] = 0
        self.allocated_node_offsets[tnode.from_id(uid)] = 0
        self.allocated_node_parents[tnode.from_id(uid)] = 0
        g_function_selector_array = self.g_function_selector.get_value(borrow=True, return_internal_type=True)
        for element in range (0, get_elements_per_type(type, self.native_modules)):
            self.allocated_elements_to_nodes[offset + element] = 0
            g_function_selector_array[offset + element] = 0
        self.g_function_selector.set_value(g_function_selector_array, borrow=True)

        n_function_selector_array = self.n_function_selector.get_value(borrow=True, return_internal_type=True)
        n_function_selector_array[offset + GEN] = NFPG_PIPE_NON
        n_function_selector_array[offset + POR] = NFPG_PIPE_NON
        n_function_selector_array[offset + RET] = NFPG_PIPE_NON
        n_function_selector_array[offset + SUB] = NFPG_PIPE_NON
        n_function_selector_array[offset + SUR] = NFPG_PIPE_NON
        n_function_selector_array[offset + CAT] = NFPG_PIPE_NON
        n_function_selector_array[offset + EXP] = NFPG_PIPE_NON
        self.n_function_selector.set_value(n_function_selector_array, borrow=True)

        # clear from name and positions dicts
        if uid in self.names:
            del self.names[uid]
        if uid in self.positions:
            del self.positions[uid]

        # hint at the free ID
        self.last_allocated_node = tnode.from_id(uid) - 1

        # remove the native module instance if there should be one
        if uid in self.native_module_instances:
            del self.native_module_instances[uid]

        # remove sensor association if there should be one
        if uid in self.inverted_sensor_map:
            sensor = self.inverted_sensor_map[uid]
            del self.inverted_sensor_map[uid]
            if sensor in self.sensormap:
                self.sensormap[sensor].remove(tnode.from_id(uid))
            if len(self.sensormap[sensor]) == 0:
                del self.sensormap[sensor]

        # remove actuator association if there should be one
        if uid in self.inverted_actuator_map:
            actuator = self.inverted_actuator_map[uid]
            del self.inverted_actuator_map[uid]
            if actuator in self.actuatormap:
                self.actuatormap[actuator].remove(tnode.from_id(uid))
            if len(self.actuatormap[actuator]) == 0:
                del self.actuatormap[actuator]

        # clear activator usage if there should be one
        used_as_activator_by = np.where(self.allocated_elements_to_activators == offset)
        if len(used_as_activator_by) > 0:
            self.allocated_elements_to_activators[used_as_activator_by] = 0

        if self.allocated_nodespaces_por_activators[parent] == tnode.from_id(uid):
            self.allocated_nodespaces_por_activators[parent] = 0
        elif self.allocated_nodespaces_ret_activators[parent] == tnode.from_id(uid):
            self.allocated_nodespaces_ret_activators[parent] = 0
        elif self.allocated_nodespaces_sub_activators[parent] == tnode.from_id(uid):
            self.allocated_nodespaces_sub_activators[parent] = 0
        elif self.allocated_nodespaces_sur_activators[parent] == tnode.from_id(uid):
            self.allocated_nodespaces_sur_activators[parent] = 0
        elif self.allocated_nodespaces_cat_activators[parent] == tnode.from_id(uid):
            self.allocated_nodespaces_cat_activators[parent] = 0
        elif self.allocated_nodespaces_exp_activators[parent] == tnode.from_id(uid):
            self.allocated_nodespaces_exp_activators[parent] = 0

    def set_nodespace_gatetype_activator(self, nodespace_uid, gate_type, activator_uid):

        activator_id = 0
        if activator_uid is not None and len(activator_uid) > 0:
            activator_id = tnode.from_id(activator_uid)

        nodespace_id = tnodespace.from_id(nodespace_uid)

        if gate_type == "por":
            self.allocated_nodespaces_por_activators[nodespace_id] = activator_id
        elif gate_type == "ret":
            self.allocated_nodespaces_ret_activators[nodespace_id] = activator_id
        elif gate_type == "sub":
            self.allocated_nodespaces_sub_activators[nodespace_id] = activator_id
        elif gate_type == "sur":
            self.allocated_nodespaces_sur_activators[nodespace_id] = activator_id
        elif gate_type == "cat":
            self.allocated_nodespaces_cat_activators[nodespace_id] = activator_id
        elif gate_type == "exp":
            self.allocated_nodespaces_exp_activators[nodespace_id] = activator_id

        nodes_in_nodespace = np.where(self.allocated_node_parents == nodespace_id)[0]
        for nid in nodes_in_nodespace:
            if self.allocated_nodes[nid] == PIPE:
                self.allocated_elements_to_activators[self.allocated_node_offsets[nid] +
                                                      get_numerical_gate_type(gate_type)] = self.allocated_node_offsets[activator_id]

    def get_nodespace(self, uid):
        if uid is None:
            uid = tnodespace.to_id(1)
        return TheanoNodespace(self, uid)

    def get_nodespace_uids(self):
        ids = [tnodespace.to_id(id) for id in np.nonzero(self.allocated_nodespaces)[0]]
        ids.append(tnodespace.to_id(1))
        return ids

    def is_nodespace(self, uid):
        return uid in self.get_nodespace_uids()

    def create_nodespace(self, parent_uid, position, name="", uid=None):

        # find a free ID / index in the allocated_nodespaces vector to hold the nodespaces's parent
        if uid is None:
            id = 0
            for i in range((self.last_allocated_nodespace + 1), self.NoNS):
                if self.allocated_nodespaces[i] == 0:
                    id = i
                    break

            if id < 1:
                for i in range(self.last_allocated_nodespace - 1):
                    if self.allocated_nodespaces[i] == 0:
                        id = i
                        break

            if id < 1:
                raise MemoryError("Cannot find free id, all " + str(self.NoNS) + " nodespace entries already in use.")
        else:
            id = tnodespace.from_id(uid)

        self.last_allocated_nodespace = id

        parent_id = 0
        if parent_uid is not None:
            parent_id = tnodespace.from_id(parent_uid)
        uid = tnodespace.to_id(id)

        self.allocated_nodespaces[id] = parent_id
        if name is not None and len(name) > 0 and name != uid:
            self.names[uid] = name
        if position is not None:
            self.positions[uid] = position

        return uid

    def delete_nodespace(self, uid):
        nodespace_id = tnodespace.from_id(uid)
        children_ids = np.where(self.allocated_nodespaces == nodespace_id)[0]
        for child_id in children_ids:
            self.delete_nodespace(tnodespace.to_id(child_id))
        node_ids = np.where(self.allocated_node_parents == nodespace_id)[0]
        for node_id in node_ids:
            self.delete_node(tnode.to_id(node_id))

        # clear from name and positions dicts
        if uid in self.names:
            del self.names[uid]
        if uid in self.positions:
            del self.positions[uid]

        self.allocated_nodespaces[nodespace_id] = 0

        self.last_allocated_nodespace = nodespace_id

    def get_sensors(self, nodespace=None, datasource=None):
        sensors = {}
        sensorlist = []
        if datasource is None:
            for ds_sensors in self.sensormap.values():
                sensorlist.extend(ds_sensors)
        elif datasource in self.sensormap:
            sensorlist = self.sensormap[datasource]
        for id in sensorlist:
            if nodespace is None or self.allocated_node_parents[id] == tnodespace.from_id(nodespace):
                uid = tnode.to_id(id)
                sensors[uid] = self.get_node(uid)
        return sensors

    def get_actors(self, nodespace=None, datatarget=None):
        actuators = {}
        actuatorlist = []
        if datatarget is None:
            for dt_actuators in self.actuatormap.values():
                actuatorlist.extend(dt_actuators)
        elif datatarget in self.actuatormap:
            actuatorlist = self.actuatormap[datatarget]
        for id in actuatorlist:
            if nodespace is None or self.allocated_node_parents[id] == tnodespace.from_id(nodespace):
                uid = tnode.to_id(id)
                actuators[uid] = self.get_node(uid)
        return actuators

    def create_link(self, source_node_uid, gate_type, target_node_uid, slot_type, weight=1, certainty=1):
        self.set_link_weight(source_node_uid, gate_type, target_node_uid, slot_type, weight)
        return True

    def set_link_weight(self, source_node_uid, gate_type, target_node_uid, slot_type, weight=1, certainty=1):

        source_nodetype = None
        target_nodetype = None
        if self.allocated_nodes[tnode.from_id(source_node_uid)] > MAX_STD_NODETYPE:
            source_nodetype = self.get_nodetype(get_string_node_type(self.allocated_nodes[tnode.from_id(source_node_uid)], self.native_modules))
        if self.allocated_nodes[tnode.from_id(target_node_uid)] > MAX_STD_NODETYPE:
            target_nodetype = self.get_nodetype(get_string_node_type(self.allocated_nodes[tnode.from_id(target_node_uid)], self.native_modules))

        ngt = get_numerical_gate_type(gate_type, source_nodetype)
        nst = get_numerical_slot_type(slot_type, target_nodetype)
        w_matrix = self.w.get_value(borrow=True)
        x = self.allocated_node_offsets[tnode.from_id(target_node_uid)] + nst
        y = self.allocated_node_offsets[tnode.from_id(source_node_uid)] + ngt
        if self.sparse:
            w_matrix[x, y] = weight
        else:
            w_matrix[x][y] = weight
        self.w.set_value(w_matrix, borrow=True)

        if slot_type == "por" and self.allocated_nodes[tnode.from_id(target_node_uid)] == PIPE:
            n_node_porlinked_array = self.n_node_porlinked.get_value(borrow=True, return_internal_type=True)
            if weight == 0:
                for g in range(7):
                    n_node_porlinked_array[self.allocated_node_offsets[tnode.from_id(target_node_uid)] + g] = 0
            else:
                for g in range(7):
                    n_node_porlinked_array[self.allocated_node_offsets[tnode.from_id(target_node_uid)] + g] = 1
            self.n_node_porlinked.set_value(n_node_porlinked_array, borrow=True)

        if slot_type == "ret" and self.allocated_nodes[tnode.from_id(target_node_uid)] == PIPE:
            n_node_retlinked_array = self.n_node_retlinked.get_value(borrow=True, return_internal_type=True)
            if weight == 0:
                for g in range(7):
                    n_node_retlinked_array[self.allocated_node_offsets[tnode.from_id(target_node_uid)] + g] = 0
            else:
                for g in range(7):
                    n_node_retlinked_array[self.allocated_node_offsets[tnode.from_id(target_node_uid)] + g] = 1
            self.n_node_retlinked.set_value(n_node_retlinked_array, borrow=True)

        return True

    def delete_link(self, source_node_uid, gate_type, target_node_uid, slot_type):
        self.set_link_weight(source_node_uid, gate_type, target_node_uid, slot_type, 0)
        return True

    def reload_native_modules(self, native_modules):
        pass

    def get_nodespace_data(self, nodespace_uid, include_links):
        data = {
            'links': {},
            'nodes': self.construct_nodes_dict(nodespace_uid, self.NoN),
            'nodespaces': self.construct_nodespaces_dict(nodespace_uid),
            'monitors': self.construct_monitors_dict()
        }
        if include_links:
            data['links'] = self.construct_links_dict(nodespace_uid)

            followupnodes = []
            for uid in data['nodes']:
                followupnodes.extend(self.get_node(uid).get_associated_node_uids())

            for uid in followupnodes:
                if self.allocated_node_parents[tnode.from_id(uid)] != tnodespace.from_id(nodespace_uid):
                    data['nodes'][uid] = self.get_node(uid).data

        if self.user_prompt is not None:
            data['user_prompt'] = self.user_prompt.copy()
            self.user_prompt = None
        return data

    def is_locked(self, lock):
        pass

    def is_locked_by(self, lock, key):
        pass

    def lock(self, lock, key, timeout=100):
        pass

    def unlock(self, lock):
        pass

    def get_modulator(self, modulator):
        pass

    def change_modulator(self, modulator, diff):
        pass

    def set_modulator(self, modulator, value):
        pass

    def get_nodetype(self, type):
        if type in self.__nodetypes:
            return self.__nodetypes[type]
        else:
            return self.native_modules.get(type)

    def construct_links_dict(self, nodespace_uid=None):
        data = {}
        if nodespace_uid is not None:
            parent = tnodespace.from_id(nodespace_uid)
        w_matrix = self.w.get_value(borrow=True)
        for source_id in np.nonzero(self.allocated_nodes)[0]:
            source_type = self.allocated_nodes[source_id]
            for gate_type in range(get_elements_per_type(source_type, self.native_modules)):
                gatecolumn = w_matrix[:, self.allocated_node_offsets[source_id] + gate_type]
                links_indices = np.nonzero(gatecolumn)[0]
                for index in links_indices:
                    target_id = self.allocated_elements_to_nodes[index]
                    if nodespace_uid is not None:
                        if self.allocated_node_parents[source_id] != parent and self.allocated_node_parents[target_id] != parent:
                            continue
                    target_type = self.allocated_nodes[target_id]
                    target_slot_numerical = index - self.allocated_node_offsets[target_id]
                    target_slot_type = get_string_slot_type(target_slot_numerical, self.get_nodetype(get_string_node_type(target_type, self.native_modules)))
                    source_gate_type = get_string_gate_type(gate_type, self.get_nodetype(get_string_node_type(source_type, self.native_modules)))
                    if self.sparse:               # sparse matrices return matrices of dimension (1,1) as values
                        weight = float(gatecolumn[index].data)
                    else:
                        weight = gatecolumn[index].item()

                    linkuid = tnode.to_id(source_id)+":"+source_gate_type+":"+target_slot_type+":"+tnode.to_id(target_id)
                    linkdata = {
                        "uid": linkuid,
                        "weight": weight,
                        "certainty": 1,
                        "source_gate_name": source_gate_type,
                        "source_node_uid": tnode.to_id(source_id),
                        "target_slot_name": target_slot_type,
                        "target_node_uid": tnode.to_id(target_id)
                    }
                    data[linkuid] = linkdata
        return data

    def construct_nodes_dict(self, nodespace_uid=None, max_nodes=-1):
        data = {}
        i = 0
        nodeids = np.nonzero(self.allocated_nodes)[0]
        if nodespace_uid is not None:
            parent_id = tnodespace.from_id(nodespace_uid)
            nodeids = np.where(self.allocated_node_parents == parent_id)[0]
        for node_id in nodeids:
            i += 1
            node_uid = tnode.to_id(node_id)
            data[node_uid] = self.get_node(node_uid).data
            if max_nodes > 0 and i > max_nodes:
                break
        return data

    def construct_nodespaces_dict(self, nodespace_uid):
        data = {}
        if nodespace_uid is None:
            nodespace_uid = self.get_nodespace(None).uid

        nodespace_id = tnodespace.from_id(nodespace_uid)
        nodespace_ids = np.nonzero(self.allocated_nodespaces)[0]
        nodespace_ids = np.append(nodespace_ids, 1)
        for candidate_id in nodespace_ids:
            is_in_hierarchy = False
            if candidate_id == nodespace_id:
                is_in_hierarchy = True
            else:
                parent_id = self.allocated_nodespaces[candidate_id]
                while parent_id > 0 and parent_id != nodespace_id:
                    parent_id = self.allocated_nodespaces[parent_id]
                if parent_id == nodespace_id:
                    is_in_hierarchy = True

            if is_in_hierarchy:
                data[tnodespace.to_id(candidate_id)] = self.get_nodespace(tnodespace.to_id(candidate_id)).data

        return data

    def construct_modulators_dict(self):
        return {}

    def get_standard_nodetype_definitions(self):
        """
        Returns the standard node types supported by this nodenet
        """
        return copy.deepcopy(STANDARD_NODETYPES)

    def set_sensors_and_actuator_feedback_to_values(self, datasource_to_value_map, datatarget_to_value_map):
        """
        Sets the sensors for the given data sources to the given values
        """

        a_array = self.a.get_value(borrow=True, return_internal_type=True)

        for datasource in datasource_to_value_map:
            value = datasource_to_value_map.get(datasource)
            sensor_uids = self.sensormap.get(datasource, [])

            for sensor_uid in sensor_uids:
                a_array[self.allocated_node_offsets[sensor_uid] + GEN] = value

        for datatarget in datatarget_to_value_map:
            value = datatarget_to_value_map.get(datatarget)
            actuator_uids = self.actuatormap.get(datatarget, [])

            for actuator_uid in actuator_uids:
                a_array[self.allocated_node_offsets[actuator_uid] + GEN] = value

        self.a.set_value(a_array, borrow=True)

    def read_actuators(self):
        """
        Returns a map of datatargets to values for writing back to the world adapter
        """

        actuator_values_to_write = {}

        a_array = self.a.get_value(borrow=True, return_internal_type=True)

        for datatarget in self.actuatormap:
            actuator_node_activations = 0
            for actuator_id in self.actuatormap[datatarget]:
                actuator_node_activations += a_array[self.allocated_node_offsets[actuator_id] + GEN]

            actuator_values_to_write[datatarget] = actuator_node_activations

        self.a.set_value(a_array, borrow=True)

        return actuator_values_to_write

    def group_nodes_by_names(self, nodespace=None, node_name_prefix=None):
        ids = []
        for uid, name in self.names.items():
            if name.startswith(node_name_prefix) and \
                    (nodespace is None or self.allocated_node_parents[tnode.from_id(uid)] == tnodespace.from_id(nodespace)):
                ids.append(uid)
        self.group_nodes_by_ids(ids, node_name_prefix)

    def group_nodes_by_ids(self, node_ids, group_name):
        ids = [tnode.from_id(uid) for uid in node_ids]
        ids = sorted(ids)
        self.nodegroups[group_name] = self.allocated_node_offsets[ids]

    def ungroup_nodes(self, group):
        if group in self.nodegroups:
            del self.nodegroups[group]

    def get_activations(self, group):
        a_array = self.a.get_value(borrow=True, return_internal_type=True)
        return a_array[self.nodegroups[group]]

    def get_thetas(self, group):
        g_theta_array = self.g_theta.get_value(borrow=True, return_internal_type=True)
        return g_theta_array[self.nodegroups[group]]

    def set_thetas(self, group, thetas):
        g_theta_array = self.g_theta.get_value(borrow=True, return_internal_type=True)
        g_theta_array[self.nodegroups[group]] = thetas
        self.g_theta.set_value(g_theta_array, borrow=True)

    def get_link_weights(self, group_from, group_to):
        w_matrix = self.w.get_value(borrow=True, return_internal_type=True)
        return w_matrix[:,self.nodegroups[group_from]][self.nodegroups[group_to]].todense()

    def set_link_weights(self, group_from, group_to, new_w):
        w_matrix = self.w.get_value(borrow=True, return_internal_type=True)
        grp_from = self.nodegroups[group_from]
        grp_to = self.nodegroups[group_to]
        cols, rows = np.meshgrid(grp_from, grp_to)
        w_matrix[rows, cols] = new_w
        self.w.set_value(w_matrix, borrow=True)

    def get_available_gatefunctions(self):
        return ["identity", "absolute", "sigmoid", "tanh", "rect", "one_over_x"]

    def rebuild_shifted(self):
        a_array = self.a.get_value(borrow=True, return_internal_type=True)
        a_rolled_array = np.roll(a_array, 7)
        a_shifted_matrix = np.lib.stride_tricks.as_strided(a_rolled_array, shape=(self.NoE, 14), strides=(self.byte_per_float, self.byte_per_float))
        self.a_shifted.set_value(a_shifted_matrix, borrow=True)
