"""
This module contains tooling to compare weights and activations
across models. Example usage::

    import copy
    import torch
    import torch.quantization.quantize_fx as quantize_fx
    import torch.ao.ns._numeric_suite_fx as ns

    m = torch.nn.Sequential(torch.nn.Conv2d(1, 1, 1)).eval()
    mp = quantize_fx.prepare_fx(m, {'': torch.quantization.default_qconfig})
    # We convert a copy because we need the original prepared model
    # to be available for comparisons, and `quantize_fx.convert_fx` is inplace.
    mq = quantize_fx.convert_fx(copy.deepcopy(mp))

    #
    # Comparing weights
    #

    # extract weight pairs
    weight_comparison = ns.extract_weights('a', mp, 'b', mq)

    # add SQNR for each comparison, inplace
    ns.extend_logger_results_with_comparison(
        weight_comparison, 'a', 'b', torch.ao.ns.fx.utils.compute_sqnr,
        'sqnr')

    # weight_comparison contains the weights from `mp` and `mq` stored
    # in pairs, and can be used for further analysis.


    #
    # Comparing activations, with error propagation
    #

    # add loggers
    mp_ns, mq_ns = ns.add_loggers(
        'a', copy.deepcopy(mp),
        'b', copy.deepcopy(mq),
        ns.OutputLogger)

    # send an example datum to capture intermediate activations
    datum = torch.randn(1, 1, 1, 1)
    mp_ns(datum)
    mq_ns(datum)

    # extract intermediate activations
    act_comparison = ns.extract_logger_info(
        mp_ns, mq_ns, ns.OutputLogger, 'b')

    # add SQNR for each comparison, inplace
    ns.extend_logger_results_with_comparison(
        act_comparison, 'a', 'b', torch.ao.ns.fx.utils.compute_sqnr,
        'sqnr')

    # act_comparison contains the activations from `mp_ns` and `mq_ns` stored
    # in pairs, and can be used for further analysis.

    #
    # Comparing activations, without error propagation
    #

    # create shadow model
    mp_shadows_mq = ns.add_shadow_loggers(
        'a', copy.deepcopy(mp),
        'b', copy.deepcopy(mq),
        ns.OutputLogger)

    # send an example datum to capture intermediate activations
    datum = torch.randn(1, 1, 1, 1)
    mp_shadows_mq(datum)

    # extract intermediate activations
    shadow_act_comparison = ns.extract_shadow_logger_info(
        mp_shadows_mq, ns.OutputLogger, 'b')

    # add SQNR for each comparison, inplace
    ns.extend_logger_results_with_comparison(
        shadow_act_comparison, 'a', 'b', torch.ao.ns.fx.utils.compute_sqnr,
        'sqnr')

    # shadow_act_comparison contains the activations from `mp_ns` and `mq_ns` stored
    # in pairs, and can be used for further analysis.

"""

import collections

import torch
import torch.nn as nn
import torch.ao.quantization.quantize_fx as quantize_fx
from torch.fx import GraphModule
from torch.fx.graph import Node
from torch.ao.ns.fx.mappings import (
    get_base_name_to_sets_of_related_ops,
)
from torch.ao.ns.fx.graph_matcher import (
    get_matching_subgraph_pairs,
    get_type_a_related_to_b,
)

from .fx.weight_utils import (
    extract_weight_from_node,
)

from .fx.graph_passes import (
    add_loggers_to_model,
    create_a_shadows_b,
)

from .fx.utils import (
    rekey_logger_info_on_node_name_of_model,
    maybe_add_missing_fqns,
    get_target_type_str,
    get_normalized_nth_input,
)

from .fx.ns_types import (
    NSSingleResultValuesType,
    NSResultsType,
    NSNodeTargetType,
)

from torch.ao.quantization import (
    QConfigMapping,
)
from torch.ao.quantization.fx.custom_config import PrepareCustomConfig
from torch.ao.quantization.backend_config.utils import get_fusion_pattern_to_root_node_getter
from torch.ao.quantization.fx.backend_config_utils import get_pattern_to_quantize_handlers
from torch.ao.quantization.fx.match_utils import find_matches
from torch.ao.quantization.fx.qconfig_mapping_utils import generate_qconfig_map
from torch.ao.ns.fx.n_shadows_utils import (
    OutputProp,
    _get_attr_name,
    _get_attr_wrapper_name,
    _get_dedup_subgraphs,
    _get_logger_for_subgraph,
    _add_logger_to_subgraph_wrapper,
    SHADOW_WRAPPER_NODE_NAME_PREFIX,
    create_submodule_from_subgraph,
)

from typing import Dict, Tuple, Callable, List, Optional, Set, Any, Type

RNNReturnType = Tuple[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]

class OutputLogger(nn.Module):
    """
    Base class for capturing intermediate values.
    """
    stats: List[torch.Tensor]
    stats_rnn: List[RNNReturnType]

    # Mark as impure so that calls to it will not be removed during DCE.
    _is_impure = True

    def __init__(
        self,
        ref_node_name: str,
        prev_node_name: str,
        model_name: str,
        ref_name: str,
        prev_node_target_type: str,
        ref_node_target_type: str,
        results_type: str,
        index_within_arg: int,
        index_of_arg: int,
        fqn: Optional[str],
        qconfig_str: Optional[str] = '',
    ):
        super().__init__()
        self.stats: List[torch.Tensor] = []
        self.stats_rnn: List[RNNReturnType] = []

        # name of the node which was responsible for adding this logger
        # Note:
        # - if we are logging node outputs, this is the same as prev_node_name
        # - if we are logging node inputs, this is the name of the node
        #   whose input this logger is logging.
        #
        # example, where logger1 is logging input of op1 and logger2 is logging
        #    the output of op1:
        #
        #  x1 -> logger1 -> op1 -> logger2 -> x2
        #
        # in this example,
        #   - logger1's prev_node_name is x1 and ref_node_name is op1
        #   - logger2's prev_node_name is op1 and ref_node_name is op1
        self.ref_node_name = ref_node_name
        # name of the node whose output this Logger is capturing
        self.prev_node_name = prev_node_name

        # name of the model from which the node originated from
        self.model_name = model_name
        # reference name, used to match loggers from separate models
        # to each other
        self.ref_name = ref_name
        # type of the target of the node whose output this logger is logging
        self.prev_node_target_type = prev_node_target_type
        # type of the target of the node which was respondible for adding this
        # logger
        self.ref_node_target_type = ref_node_target_type
        # what kind of values are inside of stats
        self.results_type = results_type
        # index of this node within the arg of the input/output node
        # for example, in cat([x1, x2, x3], dim=0), x2 would have index_within_arg == 1
        self.index_within_arg = index_within_arg
        # index of this node within the args of the input/output node
        # for example, in add(x1, x2), x2 would have index_of_arg == 1
        self.index_of_arg = index_of_arg
        # fully qualified name
        self.fqn = fqn
        # if loggers are added before prepare_fx, but we do not want
        # collect results of calibration, only results after convert_fx
        # so, we add a flag to control whether this logger collects data
        self.enabled = True
        # string representation of qconfig
        self.qconfig_str = qconfig_str

    # Note: cannot annotate the type of x because TorchScript does not support
    #   the Union type.
    def forward(self, x):
        """
        """  # blank docblock to make autodoc happy
        if not self.enabled:
            return x
        if isinstance(x, torch.Tensor):
            self.stats.append(x.detach())
        elif isinstance(x, tuple) and len(x) == 2 and len(x[1]) == 2:
            new_res = (x[0].detach(), (x[1][0].detach(), x[1][1].detach()))
            self.stats_rnn.append(new_res)
        return x

    def __repr__(self):
        return f"""OutputLogger(ref_name={self.ref_name}, model_name={self.model_name},
prev_node_name={self.prev_node_name}, ref_node_name={self.ref_node_name},
ref_node_target_type={self.ref_node_target_type}
results_type={self.results_type}, index_within_arg={self.index_within_arg},
index_of_arg={self.index_of_arg}, fqn={self.fqn})"""


class NSTracer(quantize_fx.QuantizationTracer):
    """
    Just like a regular FX quantization tracer, but treats observers and fake_quantize
    modules as leaf modules.
    """
    def is_leaf_module(self, m: torch.nn.Module, module_qualified_name : str) -> bool:
        """
        """  # blank docblock to make autodoc happy
        if isinstance(m, torch.ao.quantization.ObserverBase):
            return True
        elif isinstance(m, torch.ao.quantization.FakeQuantizeBase):
            return True
        return super().is_leaf_module(m, module_qualified_name)


def _extract_weights_one_model(
    model_name: str,
    model: GraphModule,
    nodes_and_names_to_instrument: List[Tuple[Node, str]],
    results: NSResultsType,
    op_to_type_to_weight_extraction_fn: Optional[Dict[str, Dict[Callable, Callable]]] = None,
) -> None:
    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx._extract_weights_one_model")
    for node, ref_name in nodes_and_names_to_instrument:
        res_type = NSSingleResultValuesType.WEIGHT.value
        extracted_weight = extract_weight_from_node(
            node, model, op_to_type_to_weight_extraction_fn)
        if extracted_weight:
            if ref_name not in results:
                results[ref_name] = {res_type: {}}
            results[ref_name][res_type][model_name] = [extracted_weight]


def _extract_weights_impl(
    model_name_a: str,
    gm_a: GraphModule,
    model_name_b: str,
    gm_b: GraphModule,
    base_name_to_sets_of_related_ops: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
    unmatchable_types_map: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
    op_to_type_to_weight_extraction_fn: Optional[Dict[str, Dict[Callable, Callable]]] = None,
) -> NSResultsType:
    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx._extract_weights_impl")
    matched_subgraph_pairs = get_matching_subgraph_pairs(
        gm_a, gm_b, base_name_to_sets_of_related_ops,
        unmatchable_types_map)

    # split the subgraph pairs into one data structure for each model
    nodes_and_names_to_instrument_a: List[Tuple[Node, str]] = []
    nodes_and_names_to_instrument_b: List[Tuple[Node, str]] = []
    for match_name, match in matched_subgraph_pairs.items():
        subgraph_a, subgraph_b = match
        nodes_and_names_to_instrument_a.append((subgraph_a.base_op_node, match_name))
        nodes_and_names_to_instrument_b.append((subgraph_b.base_op_node, match_name))

    # populate the results, one model at a time
    results: NSResultsType = {}
    _extract_weights_one_model(
        model_name_a, gm_a, nodes_and_names_to_instrument_a, results,
        op_to_type_to_weight_extraction_fn)
    _extract_weights_one_model(
        model_name_b, gm_b, nodes_and_names_to_instrument_b, results,
        op_to_type_to_weight_extraction_fn)

    # fill in missing fqn entries
    maybe_add_missing_fqns(results)

    # rekey on names of nodes in gm_b
    results = rekey_logger_info_on_node_name_of_model(results, model_name_b)

    return results


def extract_weights(
    model_name_a: str,
    model_a: nn.Module,
    model_name_b: str,
    model_b: nn.Module,
    base_name_to_sets_of_related_ops: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
    unmatchable_types_map: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
    op_to_type_to_weight_extraction_fn: Optional[Dict[str, Dict[Callable, Callable]]] = None,
) -> NSResultsType:
    """
    Extract weights from model A and model B, and return a comparison.

    Args:
        model_name_a: string name of model A to use in results
        model_a: model A
        model_name_b: string name of model B to use in results
        model_b: model B
        base_name_to_sets_of_related_ops: optional override of subgraph base nodes, subject to change
        unmatchable_types_map: optional override of unmatchable types, subject to change
        op_to_type_to_weight_extraction_fn: optional override of function which extracts weight
            from a type, subject to change

    Return:
        NSResultsType, containing the weight comparisons
    """

    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx.extract_weights")
    if base_name_to_sets_of_related_ops is None:
        base_name_to_sets_of_related_ops = \
            get_base_name_to_sets_of_related_ops()
    type_a_related_to_b = \
        get_type_a_related_to_b(base_name_to_sets_of_related_ops)

    # TODO(future PR): expose these
    skipped_module_names: List[str] = []
    skipped_module_classes: List[Callable] = []
    tracer_a = NSTracer(skipped_module_names, skipped_module_classes)
    tracer_b = NSTracer(skipped_module_names, skipped_module_classes)
    gm_a = GraphModule(model_a, tracer_a.trace(model_a))
    if hasattr(model_a, '_node_name_to_scope'):
        gm_a._node_name_to_scope = model_a._node_name_to_scope
    gm_b = GraphModule(model_b, tracer_b.trace(model_b))
    if hasattr(model_b, '_node_name_to_scope'):
        gm_b._node_name_to_scope = model_b._node_name_to_scope
    return _extract_weights_impl(
        model_name_a, gm_a, model_name_b, gm_b, base_name_to_sets_of_related_ops,
        unmatchable_types_map, op_to_type_to_weight_extraction_fn)


def _add_loggers_one_model(
    model_name: str,
    model: GraphModule,
    nodes_and_names_to_instrument_inputs: List[Tuple[Node, str, str]],
    nodes_and_names_to_instrument_outputs: List[Tuple[Node, str, str]],
    logger_cls: Callable,
) -> nn.Module:
    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx._add_loggers_one_model")

    # TODO(future PR): do not observe nodes we do not care
    #   about (both fp32, denylist, etc)
    node_to_instrument_inputs_to_ref_name: Dict[Node, Tuple[str, str]] = {}
    node_to_instrument_outputs_to_ref_name: Dict[Node, Tuple[str, str]] = {}
    for node, ref_name, ref_node_type in nodes_and_names_to_instrument_inputs:
        node_to_instrument_inputs_to_ref_name[node] = (ref_name, ref_node_type)
    for node, ref_name, ref_node_type in nodes_and_names_to_instrument_outputs:
        node_to_instrument_outputs_to_ref_name[node] = (ref_name, ref_node_type)

    model = add_loggers_to_model(
        model, node_to_instrument_inputs_to_ref_name,
        node_to_instrument_outputs_to_ref_name, logger_cls, model_name)
    return model


def _add_loggers_impl(
    name_a: str,
    gm_a: GraphModule,
    name_b: str,
    gm_b: GraphModule,
    logger_cls: Callable,
    should_log_inputs: bool,
    base_name_to_sets_of_related_ops: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
    unmatchable_types_map: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
) -> Tuple[nn.Module, nn.Module]:
    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx._add_loggers_impl")
    matched_subgraph_pairs = get_matching_subgraph_pairs(
        gm_a, gm_b,
        base_name_to_sets_of_related_ops, unmatchable_types_map)
    nodes_and_names_to_instrument_inputs_a = []
    nodes_and_names_to_instrument_inputs_b = []
    nodes_and_names_to_instrument_outputs_a = []
    nodes_and_names_to_instrument_outputs_b = []
    for match_name, (subgraph_a, subgraph_b) in matched_subgraph_pairs.items():
        ref_node_type_a = get_target_type_str(subgraph_a.base_op_node, gm_a)
        ref_node_type_b = get_target_type_str(subgraph_b.base_op_node, gm_b)
        # Note: for matching inputs we use start_node, such as observing
        # the input of linear in linear-relu
        if should_log_inputs:
            nodes_and_names_to_instrument_inputs_a.append(
                (subgraph_a.start_node, match_name, ref_node_type_a))
            nodes_and_names_to_instrument_inputs_b.append(
                (subgraph_b.start_node, match_name, ref_node_type_b))
        # Note: for matching activations we always use end_node,
        # such as observing the output of relu in linear-relu
        nodes_and_names_to_instrument_outputs_a.append(
            (subgraph_a.end_node, match_name, ref_node_type_a))
        nodes_and_names_to_instrument_outputs_b.append(
            (subgraph_b.end_node, match_name, ref_node_type_b))

    new_model_a = _add_loggers_one_model(
        name_a, gm_a, nodes_and_names_to_instrument_inputs_a,
        nodes_and_names_to_instrument_outputs_a, logger_cls)
    new_model_b = _add_loggers_one_model(
        name_b, gm_b, nodes_and_names_to_instrument_inputs_b,
        nodes_and_names_to_instrument_outputs_b, logger_cls)
    return (new_model_a, new_model_b)


def add_loggers(
    name_a: str,
    model_a: nn.Module,
    name_b: str,
    model_b: nn.Module,
    logger_cls: Callable,
    should_log_inputs : bool = False,
    base_name_to_sets_of_related_ops: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
    unmatchable_types_map: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
) -> Tuple[nn.Module, nn.Module]:
    """
    Instrument model A and model B with loggers.

    Args:
        model_name_a: string name of model A to use in results
        model_a: model A
        model_name_b: string name of model B to use in results
        model_b: model B
        logger_cls: class of Logger to use
        base_name_to_sets_of_related_ops: optional override of subgraph base nodes, subject to change
        unmatchable_types_map: optional override of unmatchable types, subject to change

    Return:
        Returns a tuple of (model_a_with_loggers, model_b_with_loggers).  Modifies both models inplace.
    """

    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx.add_loggers")
    # TODO(future PR): expose these
    skipped_module_names: List[str] = []
    skipped_module_classes: List[Callable] = []
    tracer_a = NSTracer(skipped_module_names, skipped_module_classes)
    tracer_b = NSTracer(skipped_module_names, skipped_module_classes)
    gm_a = GraphModule(model_a, tracer_a.trace(model_a))
    if hasattr(model_a, '_node_name_to_scope'):
        gm_a._node_name_to_scope = model_a._node_name_to_scope
    gm_b = GraphModule(model_b, tracer_b.trace(model_b))
    if hasattr(model_b, '_node_name_to_scope'):
        gm_b._node_name_to_scope = model_b._node_name_to_scope
    return _add_loggers_impl(
        name_a, gm_a, name_b, gm_b, logger_cls,
        should_log_inputs=should_log_inputs,
        base_name_to_sets_of_related_ops=base_name_to_sets_of_related_ops,
        unmatchable_types_map=unmatchable_types_map)


def _extract_logger_info_one_model(
    model: nn.Module,
    results: NSResultsType,
    logger_cls: Callable,
) -> None:
    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx._extract_logger_info_one_model")
    for gm_name, mod in model.named_modules():
        # TODO(future PR): better check when scripted
        is_logger = (
            isinstance(mod, logger_cls)  # type: ignore[arg-type]
            or (
                isinstance(mod, torch.jit.RecursiveScriptModule)
                and mod.original_name == 'OutputLogger'
            )
        )
        if is_logger:
            key = mod.ref_name
            if key not in results:
                results[key] = {}
            assert mod.model_name not in results[key], \
                f"{mod.model_name} is already present in results"
            if mod.results_type not in results[key]:
                results[key][mod.results_type] = {}
            if mod.model_name not in results[key][mod.results_type]:
                results[key][mod.results_type][mod.model_name] = []
            stats_to_use = mod.stats
            if len(mod.stats_rnn) > 0:
                stats_to_use = mod.stats_rnn
            results[key][mod.results_type][mod.model_name].append({
                'type': mod.results_type,
                'values': stats_to_use,
                'ref_node_name': mod.ref_node_name,
                'ref_node_target_type': mod.ref_node_target_type,
                'prev_node_name': mod.prev_node_name,
                'prev_node_target_type': mod.prev_node_target_type,
                'index_within_arg': mod.index_within_arg,
                'index_of_arg': mod.index_of_arg,
                'fqn': mod.fqn,
                'qconfig_str': mod.qconfig_str,
            })
            # ensure the list stays sorted
            results[key][mod.results_type][mod.model_name].sort(
                key=lambda res:
                f"{res['index_of_arg']}:{res['index_within_arg']}"
            )


# TODO(future PR): align on naming
# this is equivalent of just the comparison extraction part of `ns.compare_model_outputs`
def extract_logger_info(
    model_a: nn.Module,
    model_b: nn.Module,
    logger_cls: Callable,
    model_name_to_use_for_layer_names: str,
) -> NSResultsType:
    """
    Traverse all loggers in `model_a` and `model_b`, and extract the logged
    information.

    Args:
        model_a: model A
        model_b: model B
        logger_cls: class of Logger to use
        model_name_to_use_for_layer_names: string name of model to use for
          layer names in the output

    Return:
        NSResultsType, containing the logged comparisons
    """
    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx.extract_logger_info")
    results: NSResultsType = {}
    for model in (model_a, model_b):
        _extract_logger_info_one_model(model, results, logger_cls)
    # fill in missing fqn entries
    maybe_add_missing_fqns(results)
    # rekey on the name of model b
    results = rekey_logger_info_on_node_name_of_model(
        results, model_name_to_use_for_layer_names)
    return results


def _add_shadow_loggers_impl(
    name_a: str,
    gm_a: GraphModule,
    name_b: str,
    gm_b: GraphModule,
    logger_cls: Callable,
    should_log_inputs: bool,
    base_name_to_sets_of_related_ops: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
    node_type_to_io_type_map: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
    unmatchable_types_map: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
) -> nn.Module:
    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx._add_shadow_loggers_impl")
    matched_subgraph_pairs = get_matching_subgraph_pairs(
        gm_a, gm_b, base_name_to_sets_of_related_ops,
        unmatchable_types_map)
    gm_a_shadows_b = create_a_shadows_b(
        name_a, gm_a, name_b, gm_b, matched_subgraph_pairs, logger_cls,
        should_log_inputs=should_log_inputs,
        node_type_to_io_type_map=node_type_to_io_type_map)
    return gm_a_shadows_b


def add_shadow_loggers(
    name_a: str,
    model_a: nn.Module,
    name_b: str,
    model_b: nn.Module,
    logger_cls: Callable,
    should_log_inputs: bool = False,
    base_name_to_sets_of_related_ops: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
    node_type_to_io_type_map: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
    unmatchable_types_map: Optional[Dict[str, Set[NSNodeTargetType]]] = None,
) -> nn.Module:
    """
    Instrument model A and model B with shadow loggers.

    Args:
        model_name_a: string name of model A to use in results
        model_a: model A
        model_name_b: string name of model B to use in results
        model_b: model B
        logger_cls: class of Logger to use
        should_log_inputs: whether to log inputs
        base_name_to_sets_of_related_ops: optional override of subgraph base nodes, subject to change
        unmatchable_types_map: optional override of unmatchable types, subject to change
    """
    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx.add_shadow_loggers")
    # TODO(future PR): expose these
    skipped_module_names: List[str] = []
    skipped_module_classes: List[Callable] = []
    tracer_a = NSTracer(skipped_module_names, skipped_module_classes)
    tracer_b = NSTracer(skipped_module_names, skipped_module_classes)
    gm_a = GraphModule(model_a, tracer_a.trace(model_a))
    if hasattr(model_a, '_node_name_to_scope'):
        gm_a._node_name_to_scope = model_a._node_name_to_scope
    gm_b = GraphModule(model_b, tracer_b.trace(model_b))
    if hasattr(model_b, '_node_name_to_scope'):
        gm_b._node_name_to_scope = model_b._node_name_to_scope
    return _add_shadow_loggers_impl(
        name_a, gm_a, name_b, gm_b, logger_cls,
        should_log_inputs=should_log_inputs,
        base_name_to_sets_of_related_ops=base_name_to_sets_of_related_ops,
        node_type_to_io_type_map=node_type_to_io_type_map,
        unmatchable_types_map=unmatchable_types_map)


def extract_shadow_logger_info(
    model_a_shadows_b: nn.Module,
    logger_cls: Callable,
    model_name_to_use_for_layer_names: str,
) -> NSResultsType:
    """
    Traverse all loggers in a shadow model, and extract the logged
    information.

    Args:
        model_a_shadows_b: shadow model
        logger_cls: class of Logger to use
        model_name_to_use_for_layer_names: string name of model to use for
          layer names in the output

    Return:
        NSResultsType, containing the logged comparisons
    """
    torch._C._log_api_usage_once("quantization_api._numeric_suite_fx.extract_shadow_logger_info")
    results: NSResultsType = collections.defaultdict(dict)
    _extract_logger_info_one_model(model_a_shadows_b, results, logger_cls)
    # fill in missing fqn entries
    maybe_add_missing_fqns(results)
    # rekey on the name of model b
    results = rekey_logger_info_on_node_name_of_model(
        results, model_name_to_use_for_layer_names)
    return dict(results)


def extend_logger_results_with_comparison(
    results: NSResultsType,
    model_name_1: str,
    model_name_2: str,
    comparison_fn: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
    comparison_name: str,
) -> None:
    """
    Compares the logged values from `model_name_2` against the corresponding
    values in `model_name_1`, using `comparison_fn`. Records the result
    in `model_name_2`'s results under `comparison_name`. Modifies `results` inplace.

    Args:
        results: the result data structure from `extract_logger_info` or
          `extract_shadow_logger_info`.
        model_name_1: string name of model 1
        model_name_2: string name of model 2
        comparison_fn: function to compare two Tensors
        model_name_to_use_for_layer_names: string name of model to use for
          layer names in the output
    """
    for _, results_type_to_results in results.items():
        for _, model_name_to_results in results_type_to_results.items():
            assert model_name_1 in model_name_to_results, \
                f"{model_name_1} not found in results"
            assert model_name_2 in model_name_to_results, \
                f"{model_name_2} not found in results"

            results_1 = model_name_to_results[model_name_1]
            results_2 = model_name_to_results[model_name_2]

            for result_2 in results_2:
                index_within_arg_2 = result_2['index_within_arg']
                index_of_arg_2 = result_2['index_of_arg']
                # find corresponding result_1
                result_1 = None
                for cur_result_1 in results_1:
                    index_within_arg_1 = cur_result_1['index_within_arg']
                    index_of_arg_1 = cur_result_1['index_of_arg']
                    if (
                        (index_within_arg_1 == index_within_arg_2) and
                        (index_of_arg_1 == index_of_arg_2)
                    ):
                        result_1 = cur_result_1
                        break
                assert result_1 is not None

                values_1 = result_1['values']
                values_2 = result_2['values']
                result_2[comparison_name] = []
                for value_1, value_2 in zip(values_1, values_2):
                    comparison_result = comparison_fn(value_1, value_2)
                    result_2[comparison_name].append(comparison_result)

def prepare_n_shadows_model(
    model: torch.nn.Module,
    example_inputs: Any,
    qconfig_mappings: List[QConfigMapping],
    backend_config_dict: Any,
) -> torch.nn.Module:
    """
    Given a model with a graph such as

      x0 -> op0 -> x1 -> op1 -> x2

    and two qconfigs for op0 and two qconfigs for op1, creates a model with a
    graph such as

      x0 -> op0 -----> log_0_0 -> x1 -> op1 -----> log_1_0 -> x2
       |                           |
       | -> op0_q_0 -> log_0_1     | -> op1_q_0 -> log_1_1
       |                           |
       | -> op0_q_1 -> log_0_1     | -> op1_q_1 -> log_1_2

    where op0 is the original op0, op0_q_0 is op0 quantized with qconfig_0_0,
    op0_q_1 is op0 quantized_with qconfig_0_1, and log_0_0 is a logger recording
    the values flowing through the output of op0..

    This is useful for testing different quantization of multiple layers in
    a single pass through the model.

    High level TODOs for future PRs:
    1. add deduplication for qconfigs per subgraph
    2. figure out a better way to name the output structure
    3. if needed, optimize memory usage (currently, all intermediate activations
       are stored until results are calculated).
    4. for now, results are manually printed in an easy to read way. A logical
       next step would be to automaticaly output the best qconfig, and then
       automatically output a single model quantized with that best qconfig.
    """

    prepare_custom_config = PrepareCustomConfig()\
        .set_non_traceable_module_classes([OutputLogger])

    tracer = quantize_fx.QuantizationTracer([], [])
    mt = torch.fx.GraphModule(model, tracer.trace(model))

    # run example input propagation, we need this to call prepare_fx on
    # individual subgraphs
    output_prop = OutputProp(mt)
    output_prop.propagate(*example_inputs)

    # Find the set of subgraphs in the original graph which we need to
    # consider.
    modules = dict(mt.named_modules(remove_duplicate=False))
    patterns = get_pattern_to_quantize_handlers(backend_config_dict)
    root_node_getter_mapping = \
        get_fusion_pattern_to_root_node_getter(backend_config_dict)
    standalone_module_names: List[str] = []
    standalone_module_classes: List[Type] = []
    custom_module_classes: List[Type] = []
    matches = find_matches(
        mt.graph, modules, patterns, root_node_getter_mapping,
        standalone_module_names, standalone_module_classes, custom_module_classes)
    subgraphs_dedup = _get_dedup_subgraphs(matches)

    # generate node to qconfig for each subgraph
    # TODO(future PR): deduplicate repeating entries
    list_of_node_name_to_qconfig = []
    for qconfig_mapping in qconfig_mappings:
        node_name_to_qconfig = generate_qconfig_map(
            mt, modules, mt.graph, qconfig_mapping, tracer.node_name_to_scope)
        list_of_node_name_to_qconfig.append(node_name_to_qconfig)

    # Iterate through each quantization region in the model
    for (subgraph_idx, (match_name, nodes_in_this_subgraph)) in \
            enumerate(subgraphs_dedup.items()):
        # for now, assume that
        # 1. the first node has one input
        # 2. the last node has one output
        # 3. all the nodes are modules

        # for now, ignore all subgraphs that contain non-nodes (tuples, etc)
        # TODO(future PR): implement this
        if any(
            not isinstance(node, Node)
            for node in nodes_in_this_subgraph
        ):
            continue

        first_node = nodes_in_this_subgraph[0]
        last_node = nodes_in_this_subgraph[-1]
        # We used output propagation to populate example values on each
        # node. Use the example values from the previous node as the input
        # to the current node.
        prev_node = get_normalized_nth_input(first_node, mt, 0)
        # print('prev_node', prev_node)
        if isinstance(prev_node, list):
            example_inputs = [x.traced_result for x in prev_node]
        elif isinstance(prev_node, tuple):
            example_inputs = (x.traced_result for x in prev_node)
        else:
            # currently some ads models do not have a traced_result in every node,
            # so we have to guard for this case since we cannot quantize without
            # an example input
            # TODO(future PR): add a test case for this once we have an easy repro
            if hasattr(prev_node, 'traced_result'):
                example_inputs = (prev_node.traced_result,)  # type: ignore[attr-defined]
            else:
                print(
                    'unable to get example input for node ' +
                    f'{first_node.format_node()}, skipping')
                continue

        for subgraph_candidate_idx in range(len(qconfig_mappings) + 1):

            if subgraph_candidate_idx == 0:
                # idx = 0 is the floating point (original) version of the subgraph
                # We keep the subgraph as is, and add a logger at the end

                qconfig_str = ''
                logger_mod_orig = _get_logger_for_subgraph(
                    mt, first_node, last_node, subgraph_idx, subgraph_candidate_idx,
                    qconfig_str)

                attr_name = _get_attr_name(subgraph_idx, subgraph_candidate_idx)
                assert not hasattr(mt, attr_name)
                setattr(mt, attr_name, logger_mod_orig)
                with mt.graph.inserting_after(last_node):
                    new_node = mt.graph.call_module(attr_name, args=(last_node,), kwargs={})

            else:
                # idx > 0 means we have a candidate qconfig to try, so we need
                # to make a copy of the subgraph, feed it with the right inputs,
                # and add a logger at the end

                # get the qconfig
                # subtract one because the first candidate is the floating point
                # version of the subgraph
                node_name_to_qconfig = \
                    list_of_node_name_to_qconfig[subgraph_candidate_idx - 1]
                qconfig = node_name_to_qconfig[first_node.name]

                # if no quantization is requested, skip
                # TODO(future PR): deduplicate equivalent qconfigs that come from
                #   different qconfig mapping objects
                if qconfig is None:
                    continue

                qconfig_mapping = QConfigMapping().set_global(qconfig)

                # create a copy of the submodule, wrapped in a separate module
                orig_mod_copy_wrapped = create_submodule_from_subgraph(
                    mt, first_node, last_node)
                # print('wrapped', orig_mod_copy_wrapped)

                # add a logger to the end of this submodule
                # get first and last nodes of the submodule
                _add_logger_to_subgraph_wrapper(
                    orig_mod_copy_wrapped, subgraph_idx, subgraph_candidate_idx,
                    str(qconfig))

                # add a call to prepare_fx on the wrapper module
                orig_mod_copy_wrapped = torch.ao.quantization.quantize_fx.prepare_fx(
                    orig_mod_copy_wrapped, qconfig_mapping, example_inputs=example_inputs,
                    prepare_custom_config=prepare_custom_config)

                # attach the wrapper to the model
                attr_name = _get_attr_wrapper_name(subgraph_idx, subgraph_candidate_idx)
                assert not hasattr(mt, attr_name)
                setattr(mt, attr_name, orig_mod_copy_wrapped)

                # add a call to the wrapper module from the parent graph
                with mt.graph.inserting_before(first_node):
                    # for now, assume that the module only needs one or two
                    # inputs, and all other args/kwargs are taken care of
                    # inside the module
                    # TODO(future PR): adjust this for ops where it does not work

                    # TODO(future PR): handle fusion patterns where non-first nodes
                    # need inputs

                    # pass in all node args and kwargs
                    new_args = []
                    for arg in first_node.args:
                        if isinstance(arg, Node):
                            new_args.append(arg)
                        elif isinstance(arg, (list, tuple)) and len(arg) and isinstance(arg[0], Node):
                            for inner_arg in arg:
                                if isinstance(inner_arg, Node):
                                    new_args.append(inner_arg)

                    new_kwargs = {}
                    for name, old_kwarg in first_node.kwargs.items():
                        if isinstance(old_kwarg, Node):
                            new_kwargs[name] = old_kwarg
                        elif isinstance(old_kwarg, (list, tuple)) and len(old_kwarg):
                            for inner_old_kwarg in old_kwarg:
                                new_args.append(inner_old_kwarg)

                    new_args = tuple(new_args)  # type: ignore[assignment]

                    new_node = mt.graph.call_module(
                        attr_name, args=new_args, kwargs=new_kwargs)

    mt.recompile()
    # print(mt)
    # print(getattr(mt, 'shadow_wrapper_4_1'))
    # print(getattr(mt, 'shadow_wrapper_7_1'))
    return mt

def convert_n_shadows_model(model: GraphModule) -> GraphModule:

    for node in model.graph.nodes:
        if node.name.startswith(SHADOW_WRAPPER_NODE_NAME_PREFIX):
            orig_mod = getattr(model, node.name)
            converted_mod = torch.ao.quantization.quantize_fx.convert_fx(
                orig_mod)
            setattr(model, node.name, converted_mod)

    for name, child in model.named_modules():
        if isinstance(child, OutputLogger):
            child.enabled = True

    return model

def extract_results_n_shadows_model(model: torch.nn.Module) -> NSResultsType:
    results: NSResultsType = {}
    _extract_logger_info_one_model(model, results, OutputLogger)
    return results

def group_results_by_subgraph(results: NSResultsType) -> Any:
    """
    Creates a comparison of results

    Input:

    {
      'model': {
        'node_output': {
          'subgraph_0_0': [
            'values': [torch.tensor(...), ...], ...
            'ref_node_name': ...,
            'qconfig_str': ...,
          ],
          'subgraph_0_1': [
            'values': [torch.tensor(...), ...], ...
            'ref_node_name': ...,
            'qconfig_str': ...,
          ],
          ...
        },
      },
    }

    Output:
    {
      'subgraph_0': {
        '0': {
          'ref_node_name': '...',
          'values': [torch.tensor(...), ...],
          'qconfig_str': None,
        },
        '1': {
          'ref_node_name': '...',
          'values': [torch.tensor(...), ...],
          'qconfig_str': '...',
        },
      },
    }

    TODO(future PR): add ref_node_target_type, will be useful
    """

    subgraph_name_to_subgraph_results: Any = collections.defaultdict(dict)

    for subgraph_name_with_idx, subgraph_candidate_results in \
            results['model']['node_output'].items():

        # convert from `subgraph_m_n` to `subgraph_m` and `n`
        subgraph_str, subgraph_idx, subgraph_candidate_idx = \
            subgraph_name_with_idx.split('_')
        subgraph_name = f'{subgraph_str}_{subgraph_idx}'

        subgraph_results = {
            'ref_node_name': subgraph_candidate_results[0]['ref_node_name'],
            'values': subgraph_candidate_results[0]['values'],
            'qconfig_str': subgraph_candidate_results[0]['qconfig_str'],
        }

        subgraph_name_to_subgraph_results[subgraph_name][subgraph_candidate_idx] = \
            subgraph_results

    return dict(subgraph_name_to_subgraph_results)

def create_results_comparison(
    results_grouped,
    comparison_fn,
    comparison_fn_name,
) -> Any:
    """
    Input:

    {
      'subgraph_0': {
        '0': {
          'ref_node_name': '...',
          'values': [torch.tensor(...), ...],
          'qconfig_str': '',
        },
        '1': {
          'ref_node_name': '...',
          'values': [torch.tensor(...), ...],
          'qconfig_str': '...',
        },
      },
    }

    Output:
    {
      'subgraph_0': {
        'ref_node_name': '...',
        'candidates': {
          '1': {
            'qconfig_str': ...,
            'cmp_fn_name': ...,
            'cmp_raw': [..., ...],
            'cmp_mean': ...,
          },
          ...,
        },
      },
    }
    """

    results_comparison = {}

    for subgraph_name, subgraph_results in results_grouped.items():

        candidates = {}

        # name '0' always exists and is always the baseline
        baseline_values = subgraph_results['0']['values']

        for subgraph_inner_name, subgraph_inner_result in subgraph_results.items():
            # skip comparing baseline to baseline
            if subgraph_inner_name == '0':
                continue

            candidate_values = subgraph_inner_result['values']

            cmp_raw = []

            for baseline_val, candidate_val in zip(baseline_values, candidate_values):
                cmp_val = comparison_fn(baseline_val, candidate_val)
                cmp_raw.append(cmp_val)

            cmp_raw_tensor = torch.stack(cmp_raw)

            candidates[subgraph_inner_name] = {
                'qconfig_str': subgraph_inner_result['qconfig_str'],
                'cmp_fn_name': comparison_fn_name,
                'cmp_raw': cmp_raw_tensor,
                'cmp_mean': torch.mean(cmp_raw_tensor),
            }

        results_comparison[subgraph_name] = {
            'ref_node_name': subgraph_results['0']['ref_node_name'],
            'candidates': candidates,
        }

    return results_comparison

def print_n_shadows_summary(
    results_comparison,
) -> None:
    """
    Input:

    {
      'subgraph_0': {
        'ref_node_name': 'linear1',
        'candidates': {
          '1': {
            'qconfig_str': ...,
            'cmp_fn_name': ...,
            'cmp_raw': [45.0, 55.0],
            'cmp_mean': 50.0,
          },
          ...,
        },
      },
    }

    Prints:

    subgraph_idx | ref_node_name | best_idx | 0    | 1    | ...
    subgraph_0   | linear1       | 1        | 45.0 | 50.0 | ...
    """

    try:
        from tabulate import tabulate
    except ImportError:
        print("`print_tabular` relies on the library `tabulate`, "
              "which could not be found on this machine. Run `pip "
              "install tabulate` to install the library.")

    results = []
    for subgraph_name, subgraph_data in results_comparison.items():
        # print('sd', subgraph_data)

        mean_all_candidates = [
            candidate['cmp_mean']
            for candidate_name, candidate in subgraph_data['candidates'].items()
        ]

        # find top candidate
        # for now assume high is good and low is bad
        # TODO(future PR): make this configurable
        best_val, best_candidate = -10000.0, None
        for idx, val in enumerate(mean_all_candidates):
            if val > best_val:
                best_val = val
                best_candidate = idx + 1

        data_row = [
            subgraph_name,
            subgraph_data['ref_node_name'],
            best_candidate,
            *mean_all_candidates,
        ]
        results.append(data_row)

    max_candidate_idx_len = -1
    for data_row in results:
        max_candidate_idx_len = max(max_candidate_idx_len, len(data_row[1]))
    candidate_idx_headers = [str(x + 1) for x in range(max_candidate_idx_len)]

    headers = ['subgraph_idx', 'ref_node_name', 'best_idx', *candidate_idx_headers]
    print(tabulate(results, headers=headers))
