import datetime

import numpy
import six
from eventsourcing.infrastructure.event_store import EventStore
from eventsourcing.infrastructure.persistence_subscriber import PersistenceSubscriber
from eventsourcing.infrastructure.stored_events.base import InMemoryStoredEventRepository
from mock import Mock, MagicMock, patch
import unittest

from quantdsl.domain.model.call_dependencies import CallDependenciesRepository, CallDependencies
from quantdsl.domain.model.call_dependents import CallDependentsRepository, CallDependents
from quantdsl.domain.model.call_link import CallLinkRepository, CallLink
from quantdsl.domain.model.call_requirement import CallRequirementRepository, CallRequirement
from quantdsl.domain.model.call_result import CallResultRepository, CallResult
from quantdsl.domain.model.contract_specification import ContractSpecification, register_contract_specification
from quantdsl.domain.model.dependency_graph import DependencyGraph
from quantdsl.domain.model.market_calibration import MarketCalibration
from quantdsl.domain.model.market_simulation import MarketSimulation
from quantdsl.domain.services.dependency_graphs import generate_execution_order, get_dependency_values, \
    generate_dependency_graph
from quantdsl.domain.services.fixing_dates import regenerate_execution_order, list_fixing_dates
from quantdsl.domain.services.market_names import list_market_names
from quantdsl.domain.services.price_processes import get_price_process
from quantdsl.domain.services.simulated_prices import simulate_future_prices, generate_simulated_prices
from quantdsl.domain.services.uuids import create_uuid4
from quantdsl.exceptions import DslError
from quantdsl.infrastructure.event_sourced_repos.call_dependencies_repo import CallDependenciesRepo
from quantdsl.infrastructure.event_sourced_repos.call_dependents_repo import CallDependentsRepo
from quantdsl.infrastructure.event_sourced_repos.contract_specification_repo import ContractSpecificationRepo
from quantdsl.priceprocess.blackscholes import BlackScholesPriceProcess
from quantdsl.services import DEFAULT_PRICE_PROCESS_NAME


class TestUUIDs(unittest.TestCase):

    def test_create_uuid4(self):
        id = create_uuid4()
        self.assertIsInstance(id, six.string_types)


class TestDependencyGraph(unittest.TestCase):

    def setUp(self):
        self.es = EventStore(stored_event_repo=InMemoryStoredEventRepository())
        self.ps = PersistenceSubscriber(self.es)
        self.call_dependencies_repo = CallDependenciesRepo(self.es)
        self.call_dependents_repo = CallDependentsRepo(self.es)

    def tearDown(self):
        self.ps.close()

    def test_generate_dependency_graph_with_function_call(self):
        contract_specification = register_contract_specification(specification="""
def double(x):
    return x * 2

double(1 + 1)
""")

        generate_dependency_graph(
            contract_specification=contract_specification,
            call_dependencies_repo=self.call_dependencies_repo,
            call_dependents_repo=self.call_dependents_repo
        )

        root_dependencies = self.call_dependencies_repo[contract_specification.id]
        assert isinstance(root_dependencies, CallDependencies)
        self.assertEqual(len(root_dependencies.dependencies), 1)

        root_dependency = root_dependencies.dependencies[0]
        call_dependencies = self.call_dependencies_repo[root_dependency]
        self.assertEqual(len(call_dependencies.dependencies), 0)

        dependency = self.call_dependents_repo[root_dependency]
        assert isinstance(dependency, CallDependents)
        self.assertEqual(len(dependency.dependents), 1)

        self.assertEqual(dependency.dependents[0], contract_specification.id)

#     def test_generate_dependency_graph_recursive_functional_call(self):
#         contract_specification = self.app.register_contract_specification(specification="""
# def inc(x):
#     if x < 10:
#         return inc(x+1) + inc(x+2)
#     else:
#         return 100
#
# inc(1 + 2)
# """)
#
#         dependency_graph = self.app.generate_dependency_graph(contract_specification=contract_specification)
#
#         call_dependencies = self.app.call_dependencies_repo[dependency_graph.id]
#         self.assertEqual(len(call_dependencies.dependencies), 1)
#         dependency_id = call_dependencies.dependencies[0]
#         dependents = self.app.call_dependents_repo[dependency_id].dependents
#         self.assertEqual(len(dependents), 1)
#         self.assertIn(dependency_graph.id, dependents)
#         # A circular dependency...
#         self.assertIn(dependency_id, dependents)



    def test_generate_execution_order(self):

        # Dependencies:
        # 1 -> 2
        # 1 -> 3
        # 3 -> 2

        # Therefore dependents:
        # 1 = []
        # 2 = [1, 3]
        # 3 = [1]

        # 2 depends on nothing, so 2 is a leaf.
        # 1 depends on 3 and 2, so 1 is not next.
        # 3 depends only on 2, so is next.
        # Therefore 1 is last.
        # Hence evaluation order: 2, 3, 1

        call_dependencies_repo = MagicMock(spec=CallDependenciesRepository,
                                         __getitem__=lambda self, x: {
                                             1: [2, 3],
                                             2: [],
                                             3: [2]
                                         }[x])
        call_dependents_repo = MagicMock(spec=CallDependentsRepository,
                                         __getitem__=lambda self, x: {
                                             1: [],
                                             2: [1, 3],
                                             3: [1]
                                         }[x])

        leaf_call_ids = [2]
        execution_order_gen = generate_execution_order(leaf_call_ids, call_dependents_repo, call_dependencies_repo)
        execution_order = list(execution_order_gen)
        self.assertEqual(execution_order, [2, 3, 1])

    def test_get_dependency_values(self):
        call_dependencies_repo = MagicMock(spec=CallDependenciesRepository,
                                         __getitem__=lambda self, x: {
                                             1: CallDependencies(dependencies=[2, 3], entity_id=123, entity_version=0, timestamp=1),
                                         }[x])
        call_result_repo = MagicMock(spec=CallResultRepository,
                                     __getitem__=lambda self, x: {
                                        2: Mock(spec=CallResult, result_value=12),
                                        3: Mock(spec=CallResult, result_value=13),
                                     }[x])
        values = get_dependency_values(1, call_dependencies_repo, call_result_repo)
        self.assertEqual(values, {2: 12, 3: 13})


class TestFixingTimes(unittest.TestCase):

    def test_regenerate_execution_order(self):
        dependency_graph = Mock(spec=DependencyGraph, id=1)
        call_link_repo = MagicMock(spec=CallLinkRepository,
                                   __getitem__=lambda self, x: {
                                       1: Mock(spec=CallLink, call_id=2),
                                       2: Mock(spec=CallLink, call_id=3),
                                       3: Mock(spec=CallLink, call_id=1),
                                   }[x])
        order = regenerate_execution_order(dependency_graph.id, call_link_repo)
        order = list(order)
        self.assertEqual(order, [2, 3, 1])

    def test_list_fixing_dates(self):
        dependency_graph = Mock(spec=DependencyGraph, id=1)
        call_requirement_repo = MagicMock(spec=CallRequirementRepository,
                                   __getitem__=lambda self, x: {
                                       1: Mock(spec=CallRequirement, dsl_source="Fixing('2011-01-01', 1)"),
                                       2: Mock(spec=CallRequirement, dsl_source="Fixing('2012-02-02', 1)"),
                                       3: Mock(spec=CallRequirement, dsl_source="Fixing('2013-03-03', 1)"),
                                   }[x])
        call_link_repo = MagicMock(spec=CallLinkRepository,
                                   __getitem__=lambda self, x: {
                                       1: Mock(spec=CallLink, call_id=2),
                                       2: Mock(spec=CallLink, call_id=3),
                                       3: Mock(spec=CallLink, call_id=1),
                                   }[x])
        dates = list_fixing_dates(dependency_graph.id, call_requirement_repo, call_link_repo)
        self.assertEqual(dates, [datetime.date(2011, 1, 1), datetime.date(2012, 2, 2), datetime.date(2013, 3, 3)])


class TestMarketNames(unittest.TestCase):

    def test_list_market_names(self):
        contract_specification = Mock(spec=ContractSpecification, specification="Market('#1') + Market('#2')")
        names = list_market_names(contract_specification=contract_specification)
        self.assertEqual(names, ['#1', '#2'])


class TestSimulatedPrices(unittest.TestCase):

    @patch('quantdsl.domain.services.simulated_prices.register_simulated_price')
    @patch('quantdsl.domain.services.simulated_prices.simulate_future_prices', return_value=[
        ('#1', datetime.date(2011, 1, 1), 10),
        ('#1', datetime.date(2011, 1, 2), 10),
        ('#1', datetime.date(2011, 1, 2), 10),
    ])
    def test_generate_simulated_prices(self, simulate_future_prices, register_simuated_price):
        market_calibration = Mock(spec=MarketCalibration)
        market_simulation = Mock(spec=MarketSimulation)
        generate_simulated_prices(market_calibration=market_calibration, market_simulation=market_simulation)
        self.assertEqual(register_simuated_price.call_count, len(simulate_future_prices.return_value))

    @patch('quantdsl.domain.services.simulated_prices.get_price_process', new=lambda name: Mock(
        spec=BlackScholesPriceProcess,
        simulate_future_prices=lambda market_names, fixing_dates, observation_date, path_count, calibration_params: [
            ('#1', datetime.date(2011, 1, 1), numpy.array([ 10.])),
            ('#1', datetime.date(2011, 1, 2), numpy.array([ 10.])),
        ]
    ))
    def test_simulate_future_prices(self):
        ms = Mock(spec=MarketSimulation)
        mc = Mock(spec=MarketCalibration)
        prices = simulate_future_prices(market_simulation=ms, market_calibration=mc)
        self.assertEqual(list(prices), [
            ('#1', datetime.date(2011, 1, 1), numpy.array([ 10.])),
            ('#1', datetime.date(2011, 1, 2), numpy.array([ 10.])),
        ])


class TestPriceProcesses(unittest.TestCase):

    def test_get_price_process(self):
        price_process = get_price_process(DEFAULT_PRICE_PROCESS_NAME)
        self.assertIsInstance(price_process, BlackScholesPriceProcess)

        # Test the error paths.
        # - can't import the Python module
        self.assertRaises(DslError, get_price_process, 'x' + DEFAULT_PRICE_PROCESS_NAME)
        # - can't find the price process class
        self.assertRaises(DslError, get_price_process, DEFAULT_PRICE_PROCESS_NAME + 'x')
