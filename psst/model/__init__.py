import logging
import warnings

import pandas as pd

import pyomo.environ as env

from .model import (create_model, initialize_buses,
                initialize_time_periods, initialize_model, Suffix
                    )
from .network import (initialize_network, derive_network, calculate_network_parameters, enforce_thermal_limits)
from .generators import (initialize_generators, initialize_thermal_generators, initialize_renewable_generators,
                            initialize_hydro_generators, initial_state, maximum_minimum_power_output_generators,
                        ramp_up_ramp_down_limits, start_up_shut_down_ramp_limits, minimum_up_minimum_down_time,
                        fuel_cost, piece_wise_linear_cost,
                        production_cost, minimum_production_cost,
                        hot_start_cold_start_costs,
                        forced_outage,
                        generator_bus_contribution_factor)

from .reserves import (initialize_global_reserves, initialize_spinning_reserves, 
                       initialize_regulating_reserves_requirement, initialize_regulating_reserves,
                       initialize_flexible_ramp_reserves, initialize_flexible_ramp, initialize_zonal_reserves)
                       
from .demand import (initialize_demand)

from .constraints import (constraint_total_demand, constraint_net_power,
                        constraint_load_generation_mismatch,
                        constraint_power_balance,
                        constraint_reserves,
                        constraint_generator_power,
                        constraint_up_down_time, 
                        constraint_line, 
                        constraint_for_cost,
                        objective_function,
                        constraint_for_Flexible_Ramping,
                        )

from ..solver import solve_model, PSSTResults
from ..case.utils import calculate_PTDF

logger = logging.getLogger(__file__)


def build_model(case,
                generator_df=None,
                renew_gen_df=None,
                hydro_gen_df=None,
                load_df=None,
                branch_df=None,
                bus_df=None,
                previous_unit_commitment_df=None,
                base_MVA=None,
                generator_status=None,
                reserve_factor=None,
                regulating_reserve_factor=None,
                flexible_ramp_factor=None,
                config=None):

    if base_MVA is None:
        base_MVA = case.baseMVA

    # Configuration
    if config is None:
        config = dict()

    # Get configuration parameters from dictionary
    use_ptdf = config.pop('use_ptdf', False)
    segments = config.pop('segments', 2)
    # reserve_price = config.pop('reserve_price',  0)
    has_global_reserves = config.pop("has_global_reserves", True)
    resolve = config.pop("resolve", False)
    market_type = config.pop("market_type", "day_ahead") # real_time, hourly_ahead
    
    if reserve_factor is None:
        reserve_factor = 0

    if regulating_reserve_factor is None:
        reserve_factor = 0
        
    if flexible_ramp_factor is None:
        flexible_ramp_factor = 0         
        
    # Get case data
    generator_df = generator_df
    renew_gen_df = renew_gen_df
    hydro_gen_df=hydro_gen_df
    load_df = load_df 
    branch_df = branch_df 
    bus_df = bus_df 

    branch_df.index = branch_df.index.astype(object)
    generator_df.index = generator_df.index.astype(object)
    renew_gen_df.index = renew_gen_df.index.astype(object) 
    hydro_gen_df.index=hydro_gen_df.index.astype(object)    
    bus_df.index = bus_df.index.astype(object)
    load_df.index = load_df.index.astype(object)

    branch_df = branch_df.astype(object)
    generator_df = generator_df.astype(object)
    renew_gen_df = renew_gen_df.astype(object)
    hydro_gen_df = hydro_gen_df.astype(object) 
    bus_df = bus_df.astype(object)
    load_df = load_df.astype(object)

    zero_generation = list(generator_df[generator_df['PMAX'] == 0].index)
    if zero_generation:
        warnings.warn("Generators with zero PMAX found: {}".format(zero_generation))
    generator_df.loc[generator_df['PMAX'] == 0, 'PMAX'] = 0

    generator_df['RAMP'] = generator_df['RAMP_10']
    
    # Build model information

    model = create_model()

    initialize_buses(model, bus_names=bus_df.index)
    initialize_time_periods(model, time_periods=list(load_df.index))

    # Build network data
    initialize_network(model, transmission_lines=list(branch_df.index), bus_from=branch_df['F_BUS'].to_dict(), bus_to=branch_df['T_BUS'].to_dict())

    lines_to = {b: list() for b in bus_df.index.unique()}
    lines_from = {b: list() for b in bus_df.index.unique()}

    for i, l in branch_df.iterrows():
        lines_from[l['F_BUS']].append(i)
        lines_to[l['T_BUS']].append(i)

    derive_network(model, lines_from=lines_from, lines_to=lines_to)
    calculate_network_parameters(model, reactance=(branch_df['BR_X'] / base_MVA).to_dict())
    enforce_thermal_limits(model, thermal_limit=branch_df['RATE_A'].to_dict())

    # Build generator data
    
    tmp_df = generator_df[generator_df["GEN_TYPE"] == "Thermal"]
    thermal_generator_names = tmp_df.index
     
    initialize_thermal_generators(model, 
                            thermal_generator_names=thermal_generator_names)

    tmp_df = generator_df[generator_df["GEN_TYPE"] == "Renewable"]
    renewable_generator_names = tmp_df.index
    
    # Build renewable time series forecast data for their maximum power output
    renew_gen_dict = dict()
    columns = renew_gen_df.columns
    for i, t in renew_gen_df.iterrows():
        for col in columns:
            renew_gen_dict[(col, i)] = t[col] 
                                                                       
    initialize_renewable_generators(model,renewable_generator_names=renewable_generator_names,renewable_gen=renew_gen_dict)

    tmp_df = generator_df[generator_df["GEN_TYPE"] == "Hydro"]
    hydro_generator_names = tmp_df.index

    # Build hydro unit time series forecast data for their maximum power output
    hydro_gen_dict = dict()
    columns = hydro_gen_df.columns
    for i, t in hydro_gen_df.iterrows():
        for col in columns:
            hydro_gen_dict[(col, i)] = t[col]             
                                    
    initialize_hydro_generators(model,hydro_generator_names=hydro_generator_names,hydro_gen=hydro_gen_dict)    

    generator_at_bus = {b: list() for b in generator_df['GEN_BUS'].unique()}

    for i, g in generator_df.iterrows():
        generator_at_bus[g['GEN_BUS']].append(i)

    initialize_generators(model,
                        generator_at_bus=generator_at_bus)
                        
    model.reserve_price = env.Param(model.Generators, default=0, initialize=generator_df["RESERVE_PRICE"].to_dict())

    model.regulation_price = env.Param(model.Generators, default=0, initialize=generator_df["REGULATION_PRICE"].to_dict())

    model.vom_price = env.Param(model.Generators, default=0, initialize=generator_df["VOM"].to_dict())
                                                            
    fuel_cost(model)
    
    
    maximum_minimum_power_output_generators(model,
                                        minimum_power_output=generator_df['PMIN'].to_dict(),
                                        maximum_power_output=generator_df['PMAX'].to_dict())

    ramp_up_ramp_down_limits(model, ramp_up_limits=generator_df['RAMP'].to_dict(), ramp_down_limits=generator_df['RAMP'].to_dict())

    start_up_shut_down_ramp_limits(model, start_up_ramp_limits=generator_df['STARTUP_RAMP'].to_dict(), shut_down_ramp_limits=generator_df['SHUTDOWN_RAMP'].to_dict())

    minimum_up_minimum_down_time(model, minimum_up_time=generator_df['MINIMUM_UP_TIME'].to_dict(), minimum_down_time=generator_df['MINIMUM_DOWN_TIME'].to_dict())

    forced_outage(model)

    generator_bus_contribution_factor(model)

    if previous_unit_commitment_df is None:
        previous_unit_commitment = dict()
        for g in generator_df.index:
            previous_unit_commitment[g] = [0] * len(load_df)
        previous_unit_commitment_df = pd.DataFrame(previous_unit_commitment)
        previous_unit_commitment_df.index = load_df.index

    diff = previous_unit_commitment_df.diff()

    initial_state_dict = dict()
    for col in diff.columns:
        s = diff[col].dropna()
        diff_s = s[s!=0]
        if diff_s.empty:
            check_row = previous_unit_commitment_df[col].head(1)
        else:
            check_row = diff_s.tail(1)

        if check_row.values == -1 or check_row.values == 0:
            initial_state_dict[col] = -1 * (len(load_df) - int(check_row.index.values))
        else:
            initial_state_dict[col] = len(load_df) - int(check_row.index.values)

    logger.debug("Initial State of generators is {}".format(initial_state_dict))

    initial_state(model, initial_state=initial_state_dict)

    # setup production cost for generators

    points = dict()
    values = dict()

    # TODO : Add segments to config

    for i, g in generator_df.iterrows():


        if g['MODEL'] == 2:

            if g['NCOST'] == 2:
                logger.debug("NCOST=2")
                if g['PMIN'] == g['PMAX']:
                    small_increment = 1
                else:
                    small_increment = 0
                points[i] = pd.np.linspace(g['PMIN'], g['PMAX'] + small_increment, num=2)
                values[i] = g['COST_0'] + g['COST_1'] * points[i]

            elif g['NCOST'] == 3:
                points[i] = pd.np.linspace(g['PMIN'], g['PMAX'], num=segments)
                values[i] = g['COST_0'] + g['COST_1'] * points[i] + g['COST_2'] * points[i] ** 2

            else:
                raise NotImplementedError("Unable to build cost function for ncost={ncost}".format(ncost=g['NCOST']))

        if g['MODEL'] == 1:

            assert g['NCOST'] != 1, "Unable to form cost curve with a single point for generator {} because NCOST = {}".format(i, g['NCOST'])

            p = pd.np.zeros(g['NCOST'])
            v = pd.np.zeros(g['NCOST'])

            for n in range(0, g['NCOST']):
                v[n] = g['COST_{}'.format(2 * n + 1)]
                p[n] = g['COST_{}'.format(2 * n)]

            points[i] = p
            values[i] = v

        if g['MODEL'] == 0:
            p = pd.np.zeros(2)
            v = pd.np.zeros(2)
            p[1] = g['PMAX']
            v[1] = 0.01
            points[i] = p
            values[i] = v

    for k, v in points.items():
        points[k] = [float(i) for i in v]
        assert len(points[k]) >= 2, "Points must be of length 2 but instead found {points} for {genco}".format(points=points[k], genco=k)
    for k, v in values.items():
        values[k] = [float(i) for i in v]
        assert len(values[k]) >= 2, "Values must be of length 2 but instead found {values} for {genco}".format(values=values[k], genco=k)

    piece_wise_linear_cost(model, points, values)

    minimum_production_cost(model)
    production_cost(model)

    # setup start up and shut down costs for generators

    hot_start_costs = generator_df['STARTUP'].to_dict()
    cold_start_costs = generator_df['STARTUP'].to_dict()
    shutdown_costs = generator_df['SHUTDOWN'].to_dict()

    hot_start_cold_start_costs(model, hot_start_costs=hot_start_costs, cold_start_costs=cold_start_costs, shutdown_cost_coefficient=shutdown_costs)

    # Build load data
    load_dict = dict()
    columns = load_df.columns
    for i, t in load_df.iterrows():
        for col in columns:
            load_dict[(col, i)] = t[col]

    initialize_demand(model, demand=load_dict)

    # Initialize Pyomo Variables
    initialize_model(model)

    initialize_global_reserves(model, reserve_factor=reserve_factor)
    initialize_spinning_reserves(model, )
    initialize_regulating_reserves_requirement(model, regulating_reserve_factor=regulating_reserve_factor)
    initialize_regulating_reserves(model, )
    initialize_flexible_ramp_reserves(model, )
    initialize_flexible_ramp(model, flexible_ramp_factor=flexible_ramp_factor)
    # initialize_zonal_reserves(model, )

    # impose Pyomo Constraints

    constraint_net_power(model)

    constraint_power_balance(model)

    constraint_total_demand(model)
    constraint_load_generation_mismatch(model)
    constraint_reserves(model)
    constraint_generator_power(model)
    constraint_up_down_time(model,resolve=resolve)
    constraint_line(model) 
    constraint_for_cost(model)

    if market_type == "day_ahead":
          constraint_for_Flexible_Ramping(model)
    elif market_type == "hourly_ahead":
          constraint_for_Flexible_Ramping(model)
    elif market_type == "real_time":
          constraint_for_Flexible_Ramping(model)
    else:
          raise NotImplementedError("Unknown market type {}".format(market_type))
  # 
    # Add objective function
    objective_function(model)

    # for t, row in generator_status.iterrows():
    #     for g, v in row.iteritems():
    #         if not pd.isnull(v):
    #             model.UnitOn[g, t].fixed = True
    #             model.UnitOn[g, t] = int(float(v))

    model.dual = Suffix(direction=Suffix.IMPORT)

    return PSSTModel(model, case=case)


class PSSTModel(object):

    def __init__(self, model, case=None, is_solved=False):
        self._case = case
        self._model = model
        self._is_solved = is_solved
        self._status = None
        self._results = None

    def __repr__(self):

        repr_string = 'status={}'.format(self._status)

        string = '<{}.{}({})>'.format(
            self.__class__.__module__,
            self.__class__.__name__,
            repr_string
        )

        return string

    def solve(self, solver='glpk', verbose=False, keepfiles=False, resolve=False,  **kwargs):

        solve_model(self._model, solver=solver, verbose=verbose, keepfiles=keepfiles, **kwargs)
        self._results = PSSTResults(self)
        
        if solver == 'xpress':
            resolve = True    
                
        if resolve:
            for t, row in self.results.unit_commitment.iterrows():
                for g, v in row.iteritems():
                    if not pd.isnull(v):
                        self._model.UnitOn[g, t].fixed = True
                        self._model.UnitOn[g, t] = int(float(v))
                        
            self._model.EnforceUpTimeConstraintsInitial.deactivate()
            self._model.EnforceUpTimeConstraintsSubsequent.deactivate()
            self._model.EnforceDownTimeConstraintsInitial.deactivate()
            self._model.EnforceDownTimeConstraintsSubsequent.deactivate()

            solve_model(self._model, solver=solver, verbose=verbose, keepfiles=keepfiles, is_mip=False, **kwargs)
            self._results = PSSTResults(self)

        self._status = 'solved'

    @property
    def results(self):
        return self._results
