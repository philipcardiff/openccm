########################################################################################################################
# Copyright 2024 the authors (see AUTHORS file for full list).                                                         #
#                                                                                                                      #
#                                                                                                                      #
# This file is part of OpenCCM.                                                                                        #
#                                                                                                                      #
#                                                                                                                      #
# OpenCCM is free software: you can redistribute it and/or modify it under the terms of the GNU Lesser General Public  #
# License as published by the Free Software Foundation,either version 2.1 of the License, or (at your option)          #
# any later version.                                                                                                   #
#                                                                                                                      #
# OpenCCM is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied        #
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.                                                     #
# See the GNU Lesser General Public License for more details.                                                          #
#                                                                                                                      #
# You should have received a copy of the GNU Lesser General Public License along with OpenCCM. If not, see             #
# <https://www.gnu.org/licenses/>.                                                                                     #
########################################################################################################################

r"""
Functions related to parsing the boundary and initial conditions and then generating numba compiled code of the results
and applying them to the system.
"""

from typing import List, Tuple, Dict, Callable, Set
from collections import defaultdict

import numpy as np
import sympy as sp

from sympy.abc import x, y, z, t

from ..config_functions import ConfigParser
from ..mesh import GroupedBCs
from .helper_functions import H

BC_TEMPLATE = "@njit(inline='always', cache=True)\n" + \
              "def {}(t):\n" + \
              "    return {}\n"
"""Template for generating the boudnary condition file."""


def create_boundary_conditions(c0:                  np.ndarray,
                               config_parser:       ConfigParser,
                               inlet_map:           Dict[int, List[Tuple[int, int]]],
                               grouped_bcs:         GroupedBCs,
                               Q_weights:           Dict[int, List[float]],
                               points_for_bc:       Dict[int, List[int]],
                               t0:                  float,
                               points_per_model:    int) -> None:
    """
    CSTRs need the boundary condition in their original form since the equation for the inlets is sum (Q_in * C_in).
    PFRs however need the boundary condition in derivative form since the inlets to the PFRs produce a system of
    dynamic algebraic equations (DAEs).
    The need for a DEA solver is avoided by taking the derivative of that boundary condition but this in turn
    requires that the boundary condition be differentiated.

    If `need_time_deriv_version` is `true`, the boundary conditions generated by this function will be the time derivative version.

    This function also modifies c0 to apply the initial values of the boundary conditions to the whole domain.

    Limitations: (If any are exceeded, this function will throw an error.)
    - The boundary condition cannot be a function of spatial position along the boundary.
      Currently, only uniform values (which can be time-varying) are supported.
    - The boundary condition must be differentiable in time.

    Parameters
    ----------
    * c0:               The initial condition, needed in order to properly implement boundary conditions for PFR models.
                        The BC will override the IC value for the BC nodes.
    * config_parser:    OpenCCM ConfigParser for getting settings.
    * inlet_map:        A map between the inlet ID and the ID of the PFR(s) connected to it and the connection ID.
                        Key to dictionary is inlet ID, value is a list of tuples.
                        First entry in tuple is the CSTR ID, second value is the connection ID.
    * grouped_bcs:      Helper class used for consistent numbering and lookup of boundary conditions by name.
    * Q_weights:        Mapping between BC ID and a list of weights
                        The entries for each boundary condition MUST be in the same order as points_for_bc.
    * points_for_bc:    Mapping between boundary ID and the index into the state array.
                        Entries for each BC MUST be in the same order as Q_weights.
    * t0:               The starting time, needed for updating c0 if using a PFR model.
    * points_per_model: Number of discretization points per model. A value of 1 is assumed to represent a CSTR.
    """
    specie_names = config_parser.get_list(['SIMULATION', 'specie_names'], str)
    assert len(specie_names) == c0.shape[0]

    # CSTR models will only have one discretization points per model. PFRs must have at least two (inlet and outlet).
    need_time_deriv_version = points_per_model > 1

    bc_id_to_index_map: Dict[int, List[int]] = defaultdict(list)
    for bc_id, all_grouped_indicies in inlet_map.items():
        for model_id, _ in all_grouped_indicies:
            bc_id_to_index_map[bc_id].append(model_id)

    # Internal dict used to ensure that no variable is specified multiple times for each BC.
    anti_duplicate_dict: Dict[int, List[str]] = defaultdict(list)

    # List of lines to print for the boundary conditions file
    bc_file_lines: List[str] = [
        "from math import *\n",
        "from numba import njit\n",
        "from numpy import array, ndarray\n",
        "\n"
        "import numpy as np"
        "\n",
        "\n",
    ]

    spatial_coords  = {x, y, z}
    bc_dict         = defaultdict(dict)  # For writing to file
    bc_dict_for_c0  = defaultdict(dict)  # For calculating new c0 values if using a PFR
    bcs_names_used  = set()

    bc_str = config_parser.get_item(['SIMULATION', 'boundary_conditions'], str)
    for bc_line in bc_str.splitlines():
        specie, bc_info = [item.strip() for item in bc_line.split(':')]
        if specie not in specie_names:
            raise ValueError(f'Unknown specie: {specie} when specifying boundary condition: {bc_line}.')

        bc_name, bc_eqn_str = [item.strip() for item in bc_info.split('->')]
        bcs_names_used.add(bc_name)
        if bc_name in grouped_bcs.no_flux_names:
            raise ValueError(f'BC value specified for the no-flux bc {bc_name}.')

        bc_id = grouped_bcs.id(bc_name)
        if specie in anti_duplicate_dict[bc_id]:
            raise ValueError(f"Specie {specie} has multiple BCs specified for boundary {bc_name}.")
        else:
            anti_duplicate_dict[bc_id].append(specie)

        bc_eqn = sp.parse_expr(bc_eqn_str, local_dict={"H": H})
        bc_eqn_args = bc_eqn.free_symbols
        # Find out if it uses x, y, or z throw an error.
        if len(spatial_coords.intersection(bc_eqn_args)) > 0:
            raise ValueError(f"Boundary condition {bc_eqn} is written in terms of a spatial coordinate (x, y, z).")
        if bc_eqn_args != {t} and len(bc_eqn_args) != 0:
            raise ValueError(f"Boundary condition {bc_eqn} uses a variable other than t (time).")

        # Take time derivative if needed, and save.
        if not need_time_deriv_version:
            bc_dict[bc_id][specie] = parse_piecewise_heaviside_into_string(str(bc_eqn))
        else:
            bc_diff = bc_eqn.diff(t)
            bc_dict[bc_id][specie] = parse_piecewise_heaviside_into_string(str(bc_diff))
            bc_dict_for_c0[bc_id][specie] = bc_eqn

            # Zero out c0 for species that have any values specified for a given BC.
            c0[specie_names.index(specie), points_for_bc[bc_id]] = 0

    # Override c0 for PFR
    if need_time_deriv_version:
        for bc_id, species_dict in bc_dict_for_c0.items():
            for specie, bc_eqn in species_dict.items():
                np.add.at(c0[specie_names.index(specie)], points_for_bc[bc_id], np.array(Q_weights[bc_id]) * float(bc_eqn.evalf(subs={'t': t0})))

    bcs_names_used = sorted(bcs_names_used)  # Convert to list to keep order consistent

    # Generate one numpy array for each bc for the points called e.g. points_wall, points_inlet, etc.
    for bc_name in bcs_names_used:
        var_name = "points_" + bc_name
        bc_file_lines.append(f"{var_name} = array({points_for_bc[grouped_bcs.id(bc_name)]})\n")
    bc_file_lines.append("\n")

    # Generate a numpy array for each bc for the flow_weights called e.g. Q_weight_wall
    for bc_name in bcs_names_used:
        var_name = "Q_weights_" + bc_name
        bc_file_lines.append(f"{var_name} = array({Q_weights[grouped_bcs.id(bc_name)]})\n")
    bc_file_lines.append("\n")
    bc_file_lines.append("\n")

    # Generate a single function for each bc named wall_a, wall_b, inlet_c, etc.
    for bc_name in bcs_names_used:
        for specie_name, bc_eqn_str in bc_dict[grouped_bcs.id(bc_name)].items():
            bc_file_lines.append(BC_TEMPLATE.format(f"{bc_name}_{specie_name}", str(bc_eqn_str)))
            bc_file_lines.append("\n\n")

    # Hand unroll the loop to apply the BCs
    bc_file_lines.append("@njit(inline='always')  # Do not cache, _ddt will be a large matrix\n")
    bc_file_lines.append("def boundary_conditions(t: float, _ddt: ndarray) -> None:\n")
    if len(bcs_names_used) == 0:
        bc_file_lines.append('    pass  # No boundary conditions used')
    else:
        for bc_name in bcs_names_used:
            for specie_name in bc_dict[grouped_bcs.id(bc_name)]:
                bc_file_lines.append(f"    _ddt[{specie_names.index(specie_name)}, {'points_' + bc_name}] += {bc_name}_{specie_name}(t) * {'Q_weights_' + bc_name}\n")
            bc_file_lines.append("\n")

    # Write to file
    bc_file_path = config_parser.get_item(['SETUP', 'working_directory'],      str) + '/bc_code_gen.py'
    with open(bc_file_path, "w") as file:
        file.write("".join(bc_file_lines))


def load_initial_conditions(config_parser: ConfigParser, c0: np.ndarray) -> None:
    """
    Parse the string and load its value into the c0 array at the appropriate indices.

    Assumes one line per variable.
    All variables must have one initial condition specified, no more no less.

    Parameters
    ----------
    * config_parser:  OpenCCM ConfigParser for getting settings.
    * c0:             The numpy array to hold the initial condition.

    Returns
    -------
    * Nothing is returned, c0 is modified in place.
    """
    specie_names = config_parser.get_list(['SIMULATION', 'specie_names'], str)

    species_without_a_ic = set(specie_names)
    assert len(species_without_a_ic) == len(specie_names)  # Ensure no duplicates

    ic_string = config_parser.get_item(['SIMULATION', 'initial_conditions'], str)
    for ic_line in ic_string.splitlines():
        specie, ic_func_str = [item.strip() for item in ic_line.split('->')]

        if specie in species_without_a_ic:
            species_without_a_ic.remove(specie)
        else:
            if specie in specie_names:
                raise ValueError(f' Multiple initial conditions specified for specie {specie}')
            else:
                raise ValueError(f'Unknown specie: {specie} when specifying initial conditions')

        # Turn ic into an equation and apply it to c0
        c0[specie_names.index(specie), :] = eval(ic_func_str)

    assert len(species_without_a_ic) == 0  # All species must have an explicit IC to avoid something being missed


def parse_piecewise_heaviside_into_string(str_to_parse: str) -> str:
    """
    Sympy needs a PieceWise function in order to parse the input string and take its derivative

    Each Heavisde gets converted to:

        Piecewise(
            (0.0,                   t < 0),
            (1.0,                   t > 1.0),
            (0.5 - 0.5*cos(pi*t),   True)
        )

    When the time derivative is taken it gets converted to:

        Piecewise(
            (0,                 (t > 1.0) | (t < 0)),
            (pi/2*sin(pi*t),    True)
        )

    Parameters
    ----------
    * str_to_parse: String version of the smoothed Heaviside function.

    Returns
    -------
    * String version of the PieceWise version of the smoothed Heavisde function.
    """
    str_to_parse = str_to_parse.replace(" ", "")
    assert len(str_to_parse) > 0

    if 'Piecewise' not in str_to_parse:
        return str_to_parse

    new_str_fragments = []

    while len(str_to_parse) > 0:  # Parse until the whole string was consumed
        if 'Piecewise' not in str_to_parse:
            new_str_fragments.append(str_to_parse)
            break

        i_func = str_to_parse.index('Piecewise')
        i_split = i_func + len('Piecewise')
        str_left, str_right = str_to_parse[:i_func], str_to_parse[i_split:]
        new_str_fragments.append(str_left)

        i = _get_end_of_first_term(str_right)
        piecewise, str_to_parse = str_right[1:i], str_right[i+1:]

        pieces = []
        while len(piecewise) > 0:
            # Grab the first term based on parenthesis
            i = _get_end_of_first_term(piecewise)
            term, piecewise = piecewise[1:i], piecewise[i+1:]
            if len(piecewise) > 0 and piecewise[0] != ',':
                raise ValueError("Malformed string")
            piecewise = piecewise[1:]

            # Split term into expression and condition
            comma_idxs = [idx for idx, ch in enumerate(term) if ch == ',']
            assert len(comma_idxs) >= 1
            for idx in comma_idxs:
                test_left, test_right = term[:idx], term[idx+1:]
                if (test_left.count('(') == test_left.count(')')) and (test_right.count('(') == test_right.count(')')):
                    break  # Found the comma which represents the split between the two terms.
            else:
                raise ValueError("Malformed string")

            expression, condition = term[:idx], term[idx+1:]
            condition = '(' + condition + ')'
            if 'Piecewise' in expression:  # Recursive call to handle nested Piecewises.
                pieces.append([parse_piecewise_heaviside_into_string(expression), condition])
            else:
                pieces.append(['(' + expression + ')', condition ])

        count_true = 0
        conditions_for_true = []
        for i, (expr, condition) in enumerate(pieces):
            if condition == '(True)':
                count_true += 1
                idx_True = i
            else:
                conditions_for_true.append(condition)
        assert count_true == 1
        pieces[idx_True][1] = "(not (" + " | ".join(conditions_for_true) + "))"

        new_str_fragments.append('(' + ' + '.join(expr + '*' + condition for expr, condition in pieces) + ')')

    return ''.join(new_str_fragments)


def _get_end_of_first_term(str_to_parse: str) -> int:
    """
    Given a well-formed equation in a set of parentheses, return the index into str_to_parse for the closing parenthesis
    that matches the first opening parenthesis in the string.

    Args:
        str_to_parse: String representing a math equation with multiple parentheses

    Returns:
        ~: index of the closing parenthesis
    """
    assert len(str_to_parse) >= 2  # Minimum valid string, i.e. "()"
    assert str_to_parse.count('(') == str_to_parse.count(')')
    assert str_to_parse[0] == '('

    paran_stack = [str_to_parse[0]]

    # Must search right-to-left since there may be multiple independent terms within the string.
    for i in range(1, len(str_to_parse)):
        if str_to_parse[i] == '(':
            paran_stack.append('(')
        elif str_to_parse[i] == ')':
            paran_stack.pop()
            if len(paran_stack) == 0:
                return i

    raise ValueError('Could not find closing parenthesis, equation malformed.')
