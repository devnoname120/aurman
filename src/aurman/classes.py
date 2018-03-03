import logging
import os
from enum import Enum, auto
from subprocess import run, PIPE, DEVNULL
from typing import Sequence, List, Tuple, Set, Union, Dict

from pycman.config import PacmanConfig

from aurman.aur_utilities import is_devel, get_aur_info
from aurman.coloring import aurman_status, aurman_note, aurman_error, aurman_question, Colors
from aurman.own_exceptions import InvalidInput, ConnectionProblem
from aurman.utilities import strip_versioning_from_name, split_name_with_versioning, version_comparison, ask_user
from aurman.wrappers import expac, makepkg, pacman


class PossibleTypes(Enum):
    """
    Enum containing the possible types of packages
    """
    REPO_PACKAGE = auto()
    AUR_PACKAGE = auto()
    DEVEL_PACKAGE = auto()
    PACKAGE_NOT_REPO_NOT_AUR = auto()


class DepAlgoSolution:
    """
    Class used to track solutions while solving the dependency problem
    """

    def __init__(self, packages_in_solution, visited_packages, visited_names):
        self.packages_in_solution: List['Package'] = packages_in_solution  # containing the packages of the solution
        self.visited_packages: List['Package'] = visited_packages  # needed for tracking dep cycles
        self.visited_names: Set[str] = visited_names  # needed for tracking provided deps
        self.not_to_delete_deps: Set[str] = set()  # tracking deps which must not be deleted
        self.is_valid: bool = True  # may be set to False by the algorithm in case of conflicts, dep-cycles, ...
        self.dict_to_way: Dict[str, List['Package']] = {}  # needed for tracking the way the packages have been called
        self.dict_to_deps: Dict[str, Set[str]] = {}  # needed for tracking which deps are being provided by the packages
        self.dict_call_as_needed: Dict[str, bool] = {}  # needed for tracking if package may be removed
        self.installed_solution_packages: Set['Package'] = set()  # needed for tracking which packages are installed

    def solution_copy(self):
        to_return = DepAlgoSolution(self.packages_in_solution[:], self.visited_packages[:], set(self.visited_names))
        to_return.is_valid = self.is_valid
        to_return.not_to_delete_deps = set(self.not_to_delete_deps)
        for key, value in self.dict_to_way.items():
            to_return.dict_to_way[key] = value[:]
        for key, value in self.dict_to_deps.items():
            to_return.dict_to_deps[key] = set(value)
        for key, value in self.dict_call_as_needed.items():
            to_return.dict_call_as_needed[key] = value
        to_return.installed_solution_packages = set(self.installed_solution_packages)
        return to_return


class DepAlgoFoundProblems:
    """
    Base class for the possible problems which may occur during solving the dependency problem
    """

    def __init__(self):
        self.relevant_packages: Set['Package'] = set()


class DepAlgoCycle(DepAlgoFoundProblems):
    """
    Problem class for dependency cycles
    """

    def __init__(self, cycle_packages):
        super().__init__()
        self.cycle_packages: List['Package'] = cycle_packages

    def __repr__(self):
        return "Dep cycle: {}".format(
            " -> ".join([Colors.BOLD(Colors.LIGHT_MAGENTA(package)) for package in self.cycle_packages]))

    def __eq__(self, other):
        return isinstance(other, self.__class__) and frozenset(self.cycle_packages) == frozenset(other.cycle_packages)

    def __hash__(self):
        return hash(frozenset(self.cycle_packages))


class DepAlgoConflict(DepAlgoFoundProblems):
    """
    Problem class for conflicts
    """

    def __init__(self, conflicting_packages, ways_to_conflict):
        super().__init__()
        self.conflicting_packages: Set['Package'] = conflicting_packages
        self.ways_to_conflict: List[List['Package']] = ways_to_conflict

    def __repr__(self):
        return_string = "Conflicts between: {}".format(
            ", ".join([Colors.BOLD(Colors.LIGHT_MAGENTA(package)) for package in self.conflicting_packages]))

        for way_to_conflict in self.ways_to_conflict:
            return_string += "\nWay to package {}: {}".format(way_to_conflict[len(way_to_conflict) - 1], " -> ".join(
                [Colors.BOLD(Colors.LIGHT_MAGENTA(package)) for package in way_to_conflict]))

        return return_string

    def __eq__(self, other):
        return isinstance(other, self.__class__) and frozenset(self.conflicting_packages) == frozenset(
            other.conflicting_packages)

    def __hash__(self):
        return hash(frozenset(self.conflicting_packages))


class DepAlgoNotProvided(DepAlgoFoundProblems):
    """
    Problem class for dependencies without at least one provider
    """

    def __init__(self, dep_not_provided, package):
        super().__init__()
        self.dep_not_provided: str = dep_not_provided
        self.package: 'Package' = package

    def __repr__(self):
        return "Not provided: {} but needed by {}".format(Colors.BOLD(Colors.LIGHT_MAGENTA(self.dep_not_provided)),
                                                          Colors.BOLD(Colors.LIGHT_MAGENTA(self.package)))

    def __eq__(self, other):
        return isinstance(other,
                          self.__class__) and self.dep_not_provided == other.dep_not_provided and self.package == other.package

    def __hash__(self):
        return hash((self.dep_not_provided, self.package))


class Package:
    """
    Class representing Arch Linux packages
    """
    # default editor path
    default_editor_path = os.environ.get("EDITOR", os.path.join("usr", "bin", "nano"))
    # directory of the cache
    cache_dir = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser(os.path.join("~", ".cache"))),
                             "aurman")

    @staticmethod
    def user_input_to_categories(user_input: Sequence[str]) -> Tuple[Sequence[str], Sequence[str]]:
        """
        Categorizes user input in: For our AUR helper and for pacman

        :param user_input:  A sequence containing the user input as str
        :return:            Tuple containing two elements
                            First item: List containing the user input for our AUR helper
                            Second item: List containing the user input for pacman
        """
        for_us = []
        for_pacman = []
        user_input = list(set(user_input))

        found_in_aur_names = set([package.name for package in Package.get_packages_from_aur(user_input)])
        for _user_input in user_input:
            if _user_input in found_in_aur_names:
                for_us.append(_user_input)
            else:
                for_pacman.append(_user_input)

        return for_us, for_pacman

    @staticmethod
    def get_packages_from_aur(packages_names: Sequence[str]) -> List['Package']:
        """
        Generates and returns packages from the aur.
        see: https://wiki.archlinux.org/index.php/Arch_User_Repository

        :param packages_names:  The names of the packages to generate.
                                May not be empty.
        :return:                List containing the packages
        """
        aur_return = get_aur_info(packages_names)

        return_list = []

        for package_dict in aur_return:
            name = package_dict['Name']

            to_expand = {
                'name': name,
                'version': package_dict['Version'],
                'depends': package_dict.get('Depends', []),
                'conflicts': package_dict.get('Conflicts', []),
                'optdepends': package_dict.get('OptDepends', []),
                'provides': package_dict.get('Provides', []),
                'replaces': package_dict.get('Replaces', []),
                'pkgbase': package_dict['PackageBase'],
                'makedepends': package_dict.get('MakeDepends', []),
                'checkdepends': package_dict.get('CheckDepends', []),
                'groups': package_dict.get('Groups', [])
            }

            if is_devel(name):
                to_expand['type_of'] = PossibleTypes.DEVEL_PACKAGE
            else:
                to_expand['type_of'] = PossibleTypes.AUR_PACKAGE

            return_list.append(Package(**to_expand))

        return return_list

    @staticmethod
    def get_ignored_packages_names(ign_packages_names: Sequence[str], ign_groups_names: Sequence[str],
                                   upstream_system: 'System') -> Set[str]:
        """
        Returns the names of the ignored packages from the pacman.conf + the names from the command line

        :param ign_packages_names:  Names of packages to ignore
        :param ign_groups_names:    Names of groups to ignore
        :param upstream_system:     System containing the upstream packages
        :return:                    a set containing the names of the ignored packages
        """
        handler = PacmanConfig(conf="/etc/pacman.conf").initialize_alpm()

        # ignored packages names
        return_set = set(handler.ignorepkgs)
        for ign_packages_name in ign_packages_names:
            for name in ign_packages_name.split(","):
                return_set.add(name)

        # ignored groups names
        ignored_groups_names = set(handler.ignoregrps)
        for ign_groups_name in ign_groups_names:
            for name in ign_groups_name.split(","):
                ignored_groups_names.add(name)

        if not ignored_groups_names:
            return return_set

        # fetch packages names of groups to ignore
        for package_name in upstream_system.all_packages_dict:
            package = upstream_system.all_packages_dict[package_name]
            for package_group in package.groups:
                if package_group in ignored_groups_names:
                    return_set.add(package_name)

        return return_set

    @staticmethod
    def get_known_repos() -> List[str]:
        """
        Returns the known repos from the pacman.conf

        :return:    a list containing the known repos (ordered by occurrence in pacman.conf)
        """
        return [db.name for db in PacmanConfig(conf="/etc/pacman.conf").initialize_alpm().get_syncdbs()]

    @staticmethod
    def get_packages_from_expac(expac_operation: str, packages_names: Sequence[str], packages_type: PossibleTypes) -> \
            List['Package']:
        """
        Generates and returns packages from an expac query.
        see: https://github.com/falconindy/expac

        :param expac_operation:     The expac operation. "-S" or "-Q".
        :param packages_names:      The names of the packages to generate.
                                    May also be empty, so that all packages are being returned.
        :param packages_type:       The type of the packages. PossibleTypes Enum value
        :return:                    List containing the packages
        """
        if "Q" in expac_operation:
            formatting = list("nvDHoPReGw")
            repos = []
        else:
            assert "S" in expac_operation
            formatting = list("nvDHoPReGr")
            repos = Package.get_known_repos()

        expac_return = expac(expac_operation, formatting, packages_names)
        return_dict = {}

        for line in expac_return:
            splitted_line = line.split("?!")
            to_expand = {
                'name': splitted_line[0],
                'version': splitted_line[1],
                'depends': splitted_line[2].split(),
                'conflicts': splitted_line[3].split(),
                'optdepends': splitted_line[4].split(),
                'provides': splitted_line[5].split(),
                'replaces': splitted_line[6].split(),
                'groups': splitted_line[8].split()
            }

            if packages_type is PossibleTypes.AUR_PACKAGE or packages_type is PossibleTypes.DEVEL_PACKAGE:
                if is_devel(to_expand['name']):
                    type_to_set = PossibleTypes.DEVEL_PACKAGE
                else:
                    type_to_set = PossibleTypes.AUR_PACKAGE
            else:
                type_to_set = packages_type

            to_expand['type_of'] = type_to_set

            if splitted_line[7] == '(null)':
                to_expand['pkgbase'] = to_expand['name']
            else:
                to_expand['pkgbase'] = splitted_line[7]

            if "Q" in expac_operation:
                to_expand['install_reason'] = splitted_line[9]
            else:
                assert "S" in expac_operation
                to_expand['repo'] = splitted_line[9]

                if to_expand['name'] in return_dict and repos.index(return_dict[to_expand['name']].repo) > repos.index(
                        to_expand['repo']):
                    continue

            if to_expand['name'] in to_expand['conflicts']:
                to_expand['conflicts'].remove(to_expand['name'])

            return_dict[to_expand['name']] = Package(**to_expand)

        return list(return_dict.values())

    def __init__(self, name: str, version: str, depends: Sequence[str] = None, conflicts: Sequence[str] = None,
                 optdepends: Sequence[str] = None, provides: Sequence[str] = None, replaces: Sequence[str] = None,
                 pkgbase: str = None, install_reason: str = None, makedepends: Sequence[str] = None,
                 checkdepends: Sequence[str] = None, type_of: PossibleTypes = None, repo: str = None,
                 groups: Sequence[str] = None):
        self.name = name  # %n
        self.version = version  # %v
        self.depends = depends  # %D
        self.conflicts = conflicts  # %H
        self.optdepends = optdepends  # %o
        self.provides = provides  # %P
        self.replaces = replaces  # %R
        self.pkgbase = pkgbase  # %e
        self.install_reason = install_reason  # %w (only with -Q)
        self.makedepends = makedepends  # aur only
        self.checkdepends = checkdepends  # aur only
        self.type_of = type_of  # PossibleTypes Enum value
        self.repo = repo  # %r (only useful for upstream repo packages)
        self.groups = groups  # %G

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.name == other.name and self.version == other.version

    def __hash__(self):
        return hash((self.name, self.version))

    def __repr__(self):
        return "{}-{}".format(self.name, self.version)

    def relevant_deps(self, only_make_check: bool = False, only_depends: bool = False) -> List[str]:
        """
        Fetches the relevant deps of this package.
        self.depends for not aur packages,
        otherwise also self.makedepends and self.checkdepends

        :param only_make_check:     True if one only wants make and check depends
        :param only_depends:        True if one only wants depends
        :return:                    The relevant deps
        """
        to_return = []

        if self.depends is not None and not only_make_check:
            to_return.extend(self.depends)
        if self.makedepends is not None and not only_depends:
            to_return.extend(self.makedepends)
        if self.checkdepends is not None and not only_depends:
            to_return.extend(self.checkdepends)

        return list(set(to_return))

    def solutions_for_dep_problem(self, solution: 'DepAlgoSolution', found_problems: Set['DepAlgoFoundProblems'],
                                  installed_system: 'System', upstream_system: 'System',
                                  deps_to_deep_check: Set[str]) -> List['DepAlgoSolution']:
        """
        Heart of this AUR helper. Algorithm for dependency solving.
        Also checks for conflicts, dep-cycles and topologically sorts the solutions.

        :param solution:                The current solution
        :param found_problems:          A set containing found problems while searching for solutions
        :param installed_system:        The currently installed system
        :param upstream_system:         The system containing the known upstream packages
        :param deps_to_deep_check:      Set containing deps to check all possible dep providers of
        :return:                        The found solutions
        """

        def filter_solutions(solutions: Sequence['DepAlgoSolution']) -> List['DepAlgoSolution']:
            """
            Filter given solutions so that only valid solutions are left
            or in case of no valid solutions only one invalid solution

            :param solutions:   The solutions to filter
            :return:            The filtered solutions
            """
            return_solutions: List['DepAlgoSolution'] = []

            for solution in solutions:
                if not return_solutions:
                    return_solutions.append(solution)
                    continue

                first_solution = return_solutions[0]
                if first_solution.is_valid and solution.is_valid:
                    return_solutions.append(solution)
                elif first_solution.is_valid:
                    continue
                elif solution.is_valid:
                    return_solutions = [solution]

            return return_solutions

        if self in solution.installed_solution_packages:
            return [solution.solution_copy()]

        # dep cycle
        # dirty... thanks to dep cycle between mesa and libglvnd
        if self in solution.visited_packages and not (self.type_of is PossibleTypes.REPO_PACKAGE):
            # problem only relevant
            # if the solution is not already invalid
            if solution.is_valid:
                index_of_self = solution.visited_packages.index(self)
                cycle_packages = []
                for i in range(index_of_self, len(solution.visited_packages)):
                    cycle_packages.append(solution.visited_packages[i])
                cycle_packages.append(self)

                # create the problem
                cycle_problem = DepAlgoCycle(cycle_packages)
                for package in cycle_packages:
                    cycle_problem.relevant_packages.add(package)
                    cycle_problem.relevant_packages |= set(solution.dict_to_way.get(package.name, []))
                found_problems.add(cycle_problem)
            invalid_sol = solution.solution_copy()
            invalid_sol.is_valid = False
            return [invalid_sol]

        # pacman has to handle deps between repo packages
        elif self in solution.visited_packages:
            return [solution.solution_copy()]

        # copy solution and add self to visited packages
        solution: 'DepAlgoSolution' = solution.solution_copy()
        is_build_available: bool = self in solution.packages_in_solution
        own_way: List['Package'] = solution.dict_to_way.get(self.name, [])
        own_not_to_delete_deps: Set[str] = set()
        solution.visited_packages.append(self)
        current_solutions: List['DepAlgoSolution'] = [solution]

        # filter not fulfillable deps
        relevant_deps = self.relevant_deps()
        for dep in relevant_deps[:]:

            # skip since already provided
            if installed_system.provided_by(dep):
                continue

            # skip since built package available and dep is not a normal dependency
            # so it's make and/or check dep
            if is_build_available and dep not in self.relevant_deps(only_depends=True):
                continue

            # dep not fulfillable, solutions not valid
            if not upstream_system.provided_by(dep):
                for solution in current_solutions:
                    solution.is_valid = False

                # create problem
                dep_problem = DepAlgoNotProvided(dep, self)
                dep_problem.relevant_packages.add(self)
                dep_problem.relevant_packages |= set(own_way)
                found_problems.add(dep_problem)

                relevant_deps.remove(dep)

        # AND - every dep has to be fulfilled
        # we filtered the unfulfillable deps,
        # hence at least one dep providers is available
        for dep in relevant_deps:

            # skip since already provided
            if installed_system.provided_by(dep):
                continue

            # skip since built package available and dep is not a normal dependency
            # so it's make and/or check dep
            if is_build_available and dep not in self.relevant_deps(only_depends=True):
                continue

            # fetch dep providers
            dep_providers = upstream_system.provided_by(dep)
            dep_providers_names = [package.name for package in dep_providers]
            dep_stripped_name = strip_versioning_from_name(dep)

            # we only need relevant dep providers
            # deps_to_deep_check will be filled
            # when we encounter problems as dep-cycle, conflicts ...
            if dep_stripped_name in dep_providers_names and dep not in deps_to_deep_check:
                dep_providers = [package for package in dep_providers if package.name == dep_stripped_name]

            # OR - at least one of the dep providers needs to provide the dep
            finished_solutions = [solution for solution in current_solutions if dep in solution.visited_names]
            not_finished_solutions = [solution for solution in current_solutions if dep not in solution.visited_names]

            # check if dep provided by one of the packages already in a solution
            new_not_finished_solutions = []
            for solution in not_finished_solutions:
                if System(list(solution.installed_solution_packages)).provided_by(dep):
                    finished_solutions.append(solution)
                else:
                    new_not_finished_solutions.append(solution)
            not_finished_solutions = new_not_finished_solutions

            # track deps which may not be deleted
            for solution in current_solutions:
                if dep not in solution.not_to_delete_deps:
                    solution.not_to_delete_deps.add(dep)
                    own_not_to_delete_deps.add(dep)

            # calc and append new solutions
            current_solutions = finished_solutions
            # used for tracking problems
            new_problems_master: List[Set['DepAlgoFoundProblems']] = []
            found_problems_copy: Set['DepAlgoFoundProblems'] = set(found_problems)
            for solution in not_finished_solutions:

                # add dep to visited names
                # and create another container
                # for problem tracking
                solution.visited_names.add(dep)
                new_problems: List[Set['DepAlgoFoundProblems']] = []

                for dep_provider in dep_providers:
                    # way to the package being called in the current solution
                    if dep_provider.name not in solution.dict_to_way:
                        way_added = True
                        solution.dict_to_way[dep_provider.name] = own_way[:]
                        solution.dict_to_way[dep_provider.name].append(self)
                    else:
                        way_added = False
                    # tracking for which deps the package being called has been chosen as provider
                    if dep_provider.name not in solution.dict_to_deps:
                        solution.dict_to_deps[dep_provider.name] = set()
                    solution.dict_to_deps[dep_provider.name].add(dep)

                    # call this function recursively on the dep provider
                    # and yield an empty found_problems set instance
                    found_problems.clear()
                    current_solutions.extend(
                        dep_provider.solutions_for_dep_problem(solution, found_problems, installed_system,
                                                               upstream_system, deps_to_deep_check))
                    # save the new problems
                    new_problems.append(set(found_problems))
                    # remove added things
                    solution.dict_to_deps[dep_provider.name].remove(dep)
                    if way_added:
                        del solution.dict_to_way[dep_provider.name]

                # reset the problems to the problems
                # we had before calling the dep
                found_problems.clear()
                for problem in found_problems_copy:
                    found_problems.add(problem)

                # if there is at least one valid solution
                # problems are not relevant
                # hence add an empty set containing no problems
                for problems in new_problems:
                    if not problems:
                        new_problems_master.append(set())
                        break

                # if there are problems contained in all return values
                # show them to the user
                # will most likely be unfulfillable deps in general
                else:
                    prob_in_all_ret = set.intersection(*new_problems)
                    if prob_in_all_ret:
                        new_problems_master.append(prob_in_all_ret)
                    # otherwise append all found problems
                    else:
                        new_problems_master.append(set.union(*new_problems))

            # again - at least one valid solution
            # means new problems are not relevant
            if not_finished_solutions:
                for problems in new_problems_master:
                    if not problems:
                        break
                else:
                    for problem in set.union(*new_problems_master):
                        found_problems.add(problem)

            # filter solutions so that irrelevant solutions are not being
            # used anymore
            # great impact on the performance
            current_solutions = filter_solutions(current_solutions)

        # conflict checking
        for solution in current_solutions:
            # as with dep cycles,
            # conflicts are only relevant
            # if the solution is not already invalid
            if not solution.is_valid:
                continue

            # generate hypothetic system containing the packages of the current solution
            # and check for conflicts with that system
            installed_packages = list(solution.installed_solution_packages)
            conf_system = System(installed_packages).conflicting_with(self)

            # if there are no conflicts, nothing will get deleted, so we may
            # safely assume that we do not get an invalid solution
            if not conf_system:
                continue

            # append the whole current solution to the currently
            # installed system
            # may be empty in case of deep_search
            packages_to_append = solution.packages_in_solution[:]
            packages_to_append.append(self)
            new_system = installed_system.hypothetical_append_packages_to_system(packages_to_append)

            # if self cannot be added, this solution
            # is clearly not valid
            if self.name not in new_system.all_packages_dict:
                is_possible = False
            else:
                is_possible = True

            # these deps have to remain provided,
            # since they are needed for a package which
            # has not been installed yet
            # e.g. A needs B and C, B has been solved with this algo
            # but C not, hence B must remain provided
            # otherwise A cannot be installed
            for dep in solution.not_to_delete_deps:
                if not is_possible or not new_system.provided_by(dep):
                    is_possible = False
                    break

            # same for packages which have to remain installed
            for package in installed_packages:
                if not is_possible or \
                        solution.dict_call_as_needed.get(package.name, False) \
                        and package.name not in new_system.all_packages_dict:
                    break

            # solution possible at this point if there are no installed packages
            else:
                # check which packages have been removed
                # due to adding the packages
                for package in installed_packages:
                    # remove all remainings of the package
                    # besides the knowledge that the package
                    # has already been built
                    if package.name not in new_system.all_packages_dict:
                        solution.installed_solution_packages.remove(package)
                        if package.name in solution.dict_to_deps:
                            for dep in solution.dict_to_deps[package.name]:
                                solution.visited_names.remove(dep)
                            del solution.dict_to_deps[package.name]
                        if package.name in solution.dict_to_way:
                            del solution.dict_to_way[package.name]

                # for the case that there are no installed packages
                if is_possible:
                    continue

            # solution not possible!
            solution.is_valid = False
            conflicting_packages = set(conf_system)
            conflicting_packages.add(self)
            ways_to_conflict = []
            for package in conflicting_packages:
                way_to_conflict = solution.dict_to_way.get(package.name, [])[:]
                way_to_conflict.append(package)
                ways_to_conflict.append(way_to_conflict)

            # create the problem
            conflict_problem = DepAlgoConflict(conflicting_packages, ways_to_conflict)
            for way_to_conflict in ways_to_conflict:
                for package in way_to_conflict:
                    conflict_problem.relevant_packages.add(package)
            found_problems.add(conflict_problem)

        # we have valid solutions left, so the problems are not relevant
        if [solution for solution in current_solutions if solution.is_valid]:
            found_problems.clear()

        # add self to packages in solution, those are always topologically sorted
        for solution in current_solutions:
            solution.not_to_delete_deps -= own_not_to_delete_deps
            solution.installed_solution_packages.add(self)
            solution.packages_in_solution.append(self)
            solution.visited_packages.remove(self)

        # may contain invalid solutions !!!
        # but also filtered
        return filter_solutions(current_solutions)

    @staticmethod
    def dep_solving(packages: Sequence['Package'], installed_system: 'System', upstream_system: 'System') -> List[
        List['Package']]:
        """
        Solves deps for packages.

        :param packages:                The packages in a sequence
        :param installed_system:        The system containing the installed packages
        :param upstream_system:         The system containing the known upstream packages
        :return:                        A list containing the solutions.
                                        Every inner list contains the packages for the solution topologically sorted
        """

        deps_to_deep_check = set()
        single_first = False

        while True:
            current_solutions = [DepAlgoSolution([], [], set())]
            found_problems = set()

            # calc solutions
            # for every single package first
            if single_first:
                for package in packages:
                    new_solutions = []
                    for solution in current_solutions:
                        solution.dict_call_as_needed = {package.name: True}
                        new_solutions.extend(
                            package.solutions_for_dep_problem(solution, found_problems, installed_system,
                                                              upstream_system, deps_to_deep_check))
                    current_solutions = new_solutions

            # now for all packages together
            for solution in current_solutions:
                solution.dict_call_as_needed = {}
                for package in packages:
                    solution.dict_call_as_needed[package.name] = True
            for package in packages:
                new_solutions = []
                for solution in current_solutions:
                    new_solutions.extend(
                        package.solutions_for_dep_problem(solution, found_problems, installed_system, upstream_system,
                                                          deps_to_deep_check))
                current_solutions = new_solutions

            # delete invalid solutions
            current_solutions = [solution for solution in current_solutions if solution.is_valid]

            # in case of at least one solution, we are done
            if current_solutions:
                break

            deps_to_deep_check_length = len(deps_to_deep_check)
            for problem in found_problems:
                problem_packages_names = set([package.name for package in problem.relevant_packages])
                deps_to_deep_check |= problem_packages_names

            # if there are no new deps to deep check, we are done, too
            if len(deps_to_deep_check) == deps_to_deep_check_length and single_first:
                break
            elif len(deps_to_deep_check) == deps_to_deep_check_length:
                if len(packages) > 1:
                    single_first = True
                else:
                    break

        # output for user
        if found_problems and not current_solutions:
            aurman_error("While searching for solutions the following errors occurred:\n"
                         "{}\n".format("\n".join([aurman_note(problem, False, False) for problem in found_problems])),
                         True)

        return [solution.packages_in_solution for solution in current_solutions]

    def fetch_pkgbuild(self):
        """
        Fetches the current git aur repo changes for this package
        In cache_dir/package_base_name/.git/aurman will be copies of the last reviewed PKGBUILD and .install files
        In cache_dir/package_base_name/.git/aurman/.reviewed will be saved if the current PKGBUILD and .install files have been reviewed
        """
        import aurman.aur_utilities

        package_dir = os.path.join(Package.cache_dir, self.pkgbase)
        git_aurman_dir = os.path.join(package_dir, ".git", "aurman")
        new_loaded = True

        # check if repo has ever been fetched
        if os.path.isdir(package_dir):
            if run("git fetch", shell=True, cwd=package_dir).returncode != 0:
                logging.error("git fetch of {} failed".format(self.name))
                raise ConnectionProblem("git fetch of {} failed".format(self.name))

            head = run("git rev-parse HEAD", shell=True, stdout=PIPE, universal_newlines=True,
                       cwd=package_dir).stdout.strip()
            u = run("git rev-parse @{u}", shell=True, stdout=PIPE, universal_newlines=True,
                    cwd=package_dir).stdout.strip()

            # if new sources available
            if head != u:
                if run("git reset --hard HEAD && git pull", shell=True, stdout=DEVNULL, stderr=DEVNULL,
                       cwd=package_dir).returncode != 0:
                    logging.error("sources of {} could not be fetched".format(self.name))
                    raise ConnectionProblem("sources of {} could not be fetched".format(self.name))
            else:
                new_loaded = False

        # repo has never been fetched
        else:
            # create package dir
            if run("install -dm700 '" + package_dir + "'", shell=True, stdout=DEVNULL, stderr=DEVNULL).returncode != 0:
                logging.error("Creating package dir of {} failed".format(self.name))
                raise InvalidInput("Creating package dir of {} failed".format(self.name))

            # clone repo
            if run("git clone {}/".format(aurman.aur_utilities.aur_domain) + self.pkgbase + ".git", shell=True,
                   cwd=Package.cache_dir).returncode != 0:
                logging.error("Cloning repo of {} failed".format(self.name))
                raise ConnectionProblem("Cloning repo of {} failed".format(self.name))

        # if aurman dir does not exist - create
        if not os.path.isdir(git_aurman_dir):
            if run("install -dm700 '" + git_aurman_dir + "'", shell=True, stdout=DEVNULL,
                   stderr=DEVNULL).returncode != 0:
                logging.error("Creating git_aurman_dir of {} failed".format(self.name))
                raise InvalidInput("Creating git_aurman_dir of {} failed".format(self.name))

        # files have not yet been reviewed
        if new_loaded:
            with open(os.path.join(git_aurman_dir, ".reviewed"), "w") as f:
                f.write("0")

    def search_and_fetch_pgp_keys(self, fetch_always: bool = False, keyserver: str = None):
        """
        Searches for not imported pgp keys of this package and fetches them

        :param fetch_always:    True if the keys should be fetched without asking the user, False otherwise
        :param keyserver:       keyserver to fetch the pgp keys from
        """
        package_dir = os.path.join(Package.cache_dir, self.pkgbase)

        # if package dir does not exist - abort
        if not os.path.isdir(package_dir):
            logging.error("Package dir of {} does not exist".format(self.name))
            raise InvalidInput("Package dir of {} does not exist".format(self.name))

        pgp_keys = [line.split("=")[1].strip() for line in makepkg("--printsrcinfo", True, package_dir) if
                    "validpgpkeys =" in line]

        for pgp_key in pgp_keys:
            is_key_known = run("gpg --list-public-keys {}".format(pgp_key), shell=True, stdout=DEVNULL,
                               stderr=DEVNULL).returncode == 0
            if not is_key_known:
                if fetch_always or ask_user(
                        "PGP Key {} found in PKGBUILD of {} and is not known yet. "
                        "Do you want to import the key?".format(Colors.BOLD(Colors.LIGHT_MAGENTA(pgp_key)),
                                                                Colors.BOLD(Colors.LIGHT_MAGENTA(self.name))), True):
                    if keyserver is None:
                        if run("gpg --recv-keys {}".format(pgp_key), shell=True).returncode != 0:
                            logging.error("Import PGP key {} failed.".format(pgp_key))
                            raise ConnectionProblem("Import PGP key {} failed.".format(pgp_key))
                    else:
                        if run("gpg --keyserver {} --recv-keys {}".format(keyserver, pgp_key),
                               shell=True).returncode != 0:
                            logging.error("Import PGP key {} from {} failed.".format(pgp_key, keyserver))
                            raise ConnectionProblem("Import PGP key {} from {} failed.".format(pgp_key, keyserver))

    def show_pkgbuild(self, noedit: bool = False):
        """
        Lets the user review and edit unreviewed PKGBUILD and install files of this package

        :param noedit:     True if the user is just fine with the changes without showing them, False otherwise
        """

        package_dir = os.path.join(Package.cache_dir, self.pkgbase)
        git_aurman_dir = os.path.join(package_dir, ".git", "aurman")
        reviewed_file = os.path.join(git_aurman_dir, ".reviewed")

        # if package dir does not exist - abort
        if not os.path.isdir(package_dir):
            logging.error("Package dir of {} does not exist".format(self.name))
            raise InvalidInput("Package dir of {} does not exist".format(self.name))

        # if aurman dir does not exist - create
        if not os.path.isdir(git_aurman_dir):
            if run("install -dm700 '" + git_aurman_dir + "'", shell=True, stdout=DEVNULL,
                   stderr=DEVNULL).returncode != 0:
                logging.error("Creating git_aurman_dir of {} failed".format(self.name))
                raise InvalidInput("Creating git_aurman_dir of {} failed".format(self.name))

        # if reviewed file does not exist - create
        if not os.path.isfile(reviewed_file):
            with open(reviewed_file, "w") as f:
                f.write("0")

        # if files have been reviewed
        with open(reviewed_file, "r") as f:
            to_review = f.read().strip() == "0"

        if not to_review:
            return

        # relevant files are PKGBUILD + .install files
        relevant_files = ["PKGBUILD"]
        files_in_pack_dir = [f for f in os.listdir(package_dir) if os.path.isfile(os.path.join(package_dir, f))]
        for file in files_in_pack_dir:
            if file.endswith(".install"):
                relevant_files.append(file)

        # If the user saw any changes
        any_changes_seen = False

        # check if there are changes, if there are, ask the user if he wants to see them
        if not noedit:
            for file in relevant_files:
                if os.path.isfile(os.path.join(git_aurman_dir, file)):
                    if run("git diff --no-index --quiet '" + "' '".join(
                            [os.path.join(git_aurman_dir, file), file]) + "'",
                           shell=True, cwd=package_dir).returncode == 1:
                        if ask_user("Do you want to view the changes of {} of {}?".format(
                                Colors.BOLD(Colors.LIGHT_MAGENTA(file)), Colors.BOLD(Colors.LIGHT_MAGENTA(self.name))),
                                False):
                            run("git diff --no-index '" + "' '".join([os.path.join(git_aurman_dir, file), file]) + "'",
                                shell=True, cwd=package_dir)
                            changes_seen = True
                            any_changes_seen = True
                        else:
                            changes_seen = False
                    else:
                        changes_seen = False
                else:
                    if ask_user("Do you want to view the changes of {} of {}?".format(
                            Colors.BOLD(Colors.LIGHT_MAGENTA(file)), Colors.BOLD(Colors.LIGHT_MAGENTA(self.name))),
                            False):
                        run("git diff --no-index '" + "' '".join([os.path.join("/dev", "null"), file]) + "'",
                            shell=True,
                            cwd=package_dir)

                        changes_seen = True
                        any_changes_seen = True
                    else:
                        changes_seen = False

                # if the user wanted to see changes, ask, if he wants to edit the file
                if changes_seen:
                    if ask_user("Do you want to edit {}?".format(Colors.BOLD(Colors.LIGHT_MAGENTA(file))), False):
                        if run(Package.default_editor_path + " " + os.path.join(package_dir, file),
                               shell=True).returncode != 0:
                            logging.error("Editing {} failed".format(file))
                            raise InvalidInput("Editing {} failed".format(file))

        # if the user wants to use all files as they are now
        # copy all reviewed files to another folder for comparison of future changes
        if noedit or not any_changes_seen or ask_user(
                "Are you {} with using the files of {}?".format(Colors.BOLD(Colors.LIGHT_MAGENTA("fine")),
                                                                Colors.BOLD(Colors.LIGHT_MAGENTA(self.name))), True):
            with open(reviewed_file, "w") as f:
                f.write("1")

            for file in relevant_files:
                run("cp -f '" + "' '".join([file, os.path.join(git_aurman_dir, file)]) + "'", shell=True,
                    stdout=DEVNULL, stderr=DEVNULL, cwd=package_dir)

        else:
            logging.error("Files of {} are not okay".format(self.name))
            raise InvalidInput("Files of {} are not okay".format(self.name))

    def version_from_srcinfo(self) -> str:
        """
        Returns the version from the srcinfo
        :return:    The version read from the srcinfo
        """

        if self.pkgbase is None:
            logging.error("base package name of {} not known".format(self.name))
            raise InvalidInput("base package name of {} not known".format(self.name))

        package_dir = os.path.join(Package.cache_dir, self.pkgbase)
        if not os.path.isdir(package_dir):
            logging.error("package dir of {} does not exist".format(self.name))
            raise InvalidInput("package dir of {} does not exist".format(self.name))

        src_lines = makepkg("--printsrcinfo", True, package_dir)
        pkgver = None
        pkgrel = None
        epoch = None
        for line in src_lines:
            if "pkgver =" in line:
                pkgver = line.split("=")[1].strip()
            elif "pkgrel =" in line:
                pkgrel = line.split("=")[1].strip()
            elif "epoch =" in line:
                epoch = line.split("=")[1].strip()

        version = ""
        if epoch is not None:
            version += epoch + ":"
        if pkgver is not None:
            version += pkgver
        else:
            logging.info("version of {} must be there".format(self.name))
            raise InvalidInput("version of {} must be there".format(self.name))
        if pkgrel is not None:
            version += "-" + pkgrel

        return version

    def get_devel_version(self):
        """
        Fetches the current sources of this package.
        devel packages only!
        """

        package_dir = os.path.join(Package.cache_dir, self.pkgbase)
        makepkg("-odc --noprepare --skipinteg", False, package_dir)

        self.version = self.version_from_srcinfo()

    @staticmethod
    def get_build_dir(package_dir):
        """
        Gets the build directoy, if it is different from the package dir

        :param package_dir:     The package dir of the package
        :return:                The build dir in case there is one, the package dir otherwise
        """
        makepkg_conf = os.path.join("/etc", "makepkg.conf")
        if not os.path.isfile(makepkg_conf):
            logging.error("makepkg.conf not found")
            raise InvalidInput("makepkg.conf not found")

        with open(makepkg_conf, "r") as f:
            makepkg_conf_lines = f.read().strip().splitlines()

        for line in makepkg_conf_lines:
            line_stripped = line.strip()
            if line_stripped.startswith("PKGDEST="):
                return os.path.expandvars(os.path.expanduser(line_stripped.split("PKGDEST=")[1].strip()))
        else:
            return package_dir

    def get_package_file_to_install(self, build_dir: str, build_version: str) -> Union[str, None]:
        """
        Gets the .pkg. file of the package to install

        :param build_dir:       Build dir of the package
        :param build_version:   Build version to look for
        :return:                The name of the package file to install, None if there is none
        """
        files_in_build_dir = [f for f in os.listdir(build_dir) if os.path.isfile(os.path.join(build_dir, f))]
        for file in files_in_build_dir:
            if file.startswith(self.name + "-" + build_version + "-") and ".pkg." in \
                    file.split(self.name + "-" + build_version + "-")[1]:
                return file
        else:
            return None

    def build(self):
        """
        Build this package

        """
        # check if build needed
        build_version = self.version_from_srcinfo()
        package_dir = os.path.join(Package.cache_dir, self.pkgbase)
        build_dir = Package.get_build_dir(package_dir)

        if self.get_package_file_to_install(build_dir, build_version) is None:
            makepkg("-cf --noconfirm", False, package_dir)

    def install(self, args_as_string: str):
        """
        Install this package

        :param args_as_string: Args for pacman
        """
        build_dir = Package.get_build_dir(os.path.join(Package.cache_dir, self.pkgbase))

        # get name of package install file
        build_version = self.version_from_srcinfo()
        package_install_file = self.get_package_file_to_install(build_dir, build_version)

        if package_install_file is None:
            logging.error("package file of {} not available".format(self.name))
            raise InvalidInput("package file of {} not available".format(self.name))

        # install
        pacman("{} {}".format(args_as_string, package_install_file), False, dir_to_execute=build_dir)


class System:
    """
    Class representing a "system", which is a collection of Arch Linux packages.
    """

    @staticmethod
    def get_installed_packages() -> List['Package']:
        """
        Returns the installed packages on the system

        :return:    A list containing the installed packages
        """
        repo_packages_names = set(expac("-S", ('n',), ()))
        installed_packages_names = set(expac("-Q", ('n',), ()))
        installed_repo_packages_names = installed_packages_names & repo_packages_names
        unclassified_installed_names = installed_packages_names - installed_repo_packages_names

        return_list = []

        # installed repo packages
        if installed_repo_packages_names:
            return_list.extend(
                Package.get_packages_from_expac("-Q", list(installed_repo_packages_names), PossibleTypes.REPO_PACKAGE))

        # installed aur packages
        installed_aur_packages_names = set(
            [package.name for package in Package.get_packages_from_aur(list(unclassified_installed_names))])

        if installed_aur_packages_names:
            return_list.extend(
                Package.get_packages_from_expac("-Q", list(installed_aur_packages_names), PossibleTypes.AUR_PACKAGE))

        unclassified_installed_names -= installed_aur_packages_names

        # installed not repo not aur packages
        if unclassified_installed_names:
            return_list.extend(Package.get_packages_from_expac("-Q", list(unclassified_installed_names),
                                                               PossibleTypes.PACKAGE_NOT_REPO_NOT_AUR))

        return return_list

    @staticmethod
    def get_repo_packages() -> List['Package']:
        """
        Returns the current repo packages.

        :return:    A list containing the current repo packages
        """
        return Package.get_packages_from_expac("-S", (), PossibleTypes.REPO_PACKAGE)

    def __init__(self, packages: Sequence['Package']):
        self.all_packages_dict = {}  # names as keys and packages as values
        self.repo_packages_list = []  # list containing the repo packages
        self.aur_packages_list = []  # list containing the aur but not devel packages
        self.devel_packages_list = []  # list containing the aur devel packages
        self.not_repo_not_aur_packages_list = []  # list containing the packages that are neither repo nor aur packages

        # reverse dict for finding providings. names of providings as keys and providing packages as values in lists
        self.provides_dict = {}
        # same for conflicts
        self.conflicts_dict = {}

        self.append_packages(packages)

    def append_packages(self, packages: Sequence['Package']):
        """
        Appends packages to this system.

        :param packages:    The packages to append in a sequence
        """
        for package in packages:
            if package.name in self.all_packages_dict:
                logging.error("Package {} already known".format(package))
                raise InvalidInput("Package {} already known".format(package))

            self.all_packages_dict[package.name] = package

            if package.type_of is PossibleTypes.REPO_PACKAGE:
                self.repo_packages_list.append(package)
            elif package.type_of is PossibleTypes.AUR_PACKAGE:
                self.aur_packages_list.append(package)
            elif package.type_of is PossibleTypes.DEVEL_PACKAGE:
                self.devel_packages_list.append(package)
            else:
                assert package.type_of is PossibleTypes.PACKAGE_NOT_REPO_NOT_AUR
                self.not_repo_not_aur_packages_list.append(package)

        self.__append_to_x_dict(packages, 'provides')
        self.__append_to_x_dict(packages, 'conflicts')

    def __append_to_x_dict(self, packages: Sequence['Package'], dict_name: str):
        dict_to_append_to = getattr(self, "{}_dict".format(dict_name))

        for package in packages:
            relevant_package_values = getattr(package, dict_name)

            for relevant_value in relevant_package_values:
                value_name = strip_versioning_from_name(relevant_value)
                if value_name in dict_to_append_to:
                    dict_to_append_to[value_name].append(package)
                else:
                    dict_to_append_to[value_name] = [package]

    def provided_by(self, dep: str) -> List['Package']:
        """
        Providers for the dep

        :param dep:     The dep to be provided
        :return:        List containing the providing packages
        """

        dep_name, dep_cmp, dep_version = split_name_with_versioning(dep)
        return_list = []

        if dep_name in self.all_packages_dict:
            package = self.all_packages_dict[dep_name]
            if dep_cmp == "":
                return_list.append(package)
            elif version_comparison(package.version, dep_cmp, dep_version):
                return_list.append(package)

        if dep_name in self.provides_dict:
            possible_packages = self.provides_dict[dep_name]
            for package in possible_packages:

                if package in return_list:
                    continue

                for provide in package.provides:
                    provide_name, provide_cmp, provide_version = split_name_with_versioning(provide)

                    if provide_name != dep_name:
                        continue

                    if dep_cmp == "":
                        return_list.append(package)
                    elif (provide_cmp == "=" or provide_cmp == "==") and version_comparison(provide_version, dep_cmp,
                                                                                            dep_version):
                        return_list.append(package)
                    elif (provide_cmp == "") and version_comparison(package.version, dep_cmp, dep_version):
                        return_list.append(package)

        return return_list

    def conflicting_with(self, package: 'Package') -> List['Package']:
        """
        Returns the packages conflicting with "package"

        :param package:     The package to check for conflicts with
        :return:            List containing the conflicting packages
        """
        name = package.name
        version = package.version

        return_list = []

        if name in self.all_packages_dict:
            return_list.append(self.all_packages_dict[name])

        for conflict in package.conflicts:
            conflict_name, conflict_cmp, conflict_version = split_name_with_versioning(conflict)

            if conflict_name not in self.all_packages_dict:
                continue

            possible_conflict_package = self.all_packages_dict[conflict_name]

            if possible_conflict_package in return_list:
                continue

            if conflict_cmp == "":
                return_list.append(possible_conflict_package)
            elif version_comparison(possible_conflict_package.version, conflict_cmp, conflict_version):
                return_list.append(possible_conflict_package)

        if name in self.conflicts_dict:
            possible_conflict_packages = self.conflicts_dict[name]
            for possible_conflict_package in possible_conflict_packages:

                if possible_conflict_package in return_list:
                    continue

                for conflict in possible_conflict_package.conflicts:
                    conflict_name, conflict_cmp, conflict_version = split_name_with_versioning(conflict)

                    if conflict_name != name:
                        continue

                    if conflict_cmp == "":
                        return_list.append(possible_conflict_package)
                    elif version_comparison(version, conflict_cmp, conflict_version):
                        return_list.append(possible_conflict_package)

        return return_list

    def append_packages_by_name(self, packages_names: Sequence[str]):
        """
        Appends packages to this system by names.

        :param packages_names:          The names of the packages
        """

        packages_names = set([strip_versioning_from_name(name) for name in packages_names])
        packages_names_to_fetch = [name for name in packages_names if name not in self.all_packages_dict]

        while packages_names_to_fetch:
            fetched_packages = Package.get_packages_from_aur(packages_names_to_fetch)
            self.append_packages(fetched_packages)

            deps_of_the_fetched_packages = []
            for package in fetched_packages:
                deps_of_the_fetched_packages.extend(package.relevant_deps())

            relevant_deps = list(set([strip_versioning_from_name(dep) for dep in deps_of_the_fetched_packages]))

            packages_names_to_fetch = [dep for dep in relevant_deps if dep not in self.all_packages_dict]

    def are_all_deps_fulfilled(self, package: 'Package', only_make_check: bool = False,
                               only_depends: bool = False) -> bool:
        """
        if all deps of the package are fulfilled on the system
        :param package:             the package to check the deps of
        :param only_make_check:     True if one only wants make and check depends
        :param only_depends:        True if one only wants depends
        :return:                    True if the deps are fulfilled, False otherwise
        """

        for dep in package.relevant_deps(only_make_check=only_make_check, only_depends=only_depends):
            if not self.provided_by(dep):
                return False
        else:
            return True

    @staticmethod
    def calc_install_chunks(packages_to_chunk: Sequence['Package']) -> List[List['Package']]:
        """
        Calculates the chunks in which the given packages would be installed.
        Repo packages are installed at once, AUR packages one by one.
        e.g. AUR1, Repo1, Repo2, AUR2 yields: AUR1, Repo1 AND Repo2, AUR2

        :param packages_to_chunk:   The packages to calc the chunks of
        :return:                    The packages in chunks
        """
        current_list: List['Package'] = []
        return_list: List[List['Package']] = [current_list]

        for package in packages_to_chunk:
            if current_list and (package.type_of is not PossibleTypes.REPO_PACKAGE
                                 or current_list[0].type_of is not package.type_of):

                current_list = [package]
                return_list.append(current_list)
            else:
                current_list.append(package)

        return return_list

    def hypothetical_append_packages_to_system(self, packages: List['Package']) -> 'System':
        """
        hypothetically appends packages to this system (only makes sense for the installed system)
        and removes all conflicting packages and packages whose deps are not fulfilled anymore.

        :param packages:    the packages to append
        :return:            the new system
        """

        new_system = System(list(self.all_packages_dict.values()))
        if not packages:
            return new_system

        for package_chunk in System.calc_install_chunks(packages):
            # check if packages in chunk conflict each other
            package_chunk_system = System(())
            for package in package_chunk:
                if package_chunk_system.conflicting_with(package):
                    break
                package_chunk_system.append_packages((package,))
            # no conflicts
            else:
                # calculate conflicting packages
                conflicting_new_system_packages = []
                for package in package_chunk:
                    conflicting_new_system_packages.extend(new_system.conflicting_with(package))
                conflicting_new_system_packages = set(conflicting_new_system_packages)

                # remove conflicting packages
                if conflicting_new_system_packages:
                    deleted_packages = True
                    for package in conflicting_new_system_packages:
                        del new_system.all_packages_dict[package.name]
                    new_system = System(list(new_system.all_packages_dict.values()))
                else:
                    deleted_packages = False

                # append packages
                new_system.append_packages(package_chunk)
                if not deleted_packages:
                    continue

                # delete packages whose deps are not fulfilled anymore
                while True:
                    to_delete_packages = []
                    for package in new_system.all_packages_dict.values():
                        if not new_system.are_all_deps_fulfilled(package, only_depends=True):
                            to_delete_packages.append(package)

                    if not to_delete_packages:
                        break

                    for package in to_delete_packages:
                        del new_system.all_packages_dict[package.name]
                    new_system = System(list(new_system.all_packages_dict.values()))

        return new_system

    def differences_between_systems(self, other_systems: Sequence['System']) -> Tuple[
        Tuple[Set['Package'], Set['Package']], List[Tuple[Set['Package'], Set['Package']]]]:
        """
        Evaluates differences between this system and other systems.

        :param other_systems:   The other systems.
        :return:                A tuple containing two items:

                                First item:
                                    Tuple containing two items:

                                    First item:
                                        installed packages in respect to this system,
                                        which are in all other systems
                                    Second item:
                                        uninstalled packages in respect to this system,
                                        which are in all other systems

                                Second item:
                                    List containing tuples with two items each:

                                    For the i-th tuple (all in all as many tuples as other systems):
                                        First item:
                                            installed packages in respect to this system,
                                            which are in the i-th system but not in all systems
                                        Second item:
                                            uninstalled packages in respect to this system,
                                            which are in the i-th system but not in all systems
        """

        differences_tuples = []
        own_packages = set(self.all_packages_dict.values())

        for other_system in other_systems:
            current_difference_tuple = (set(), set())
            differences_tuples.append(current_difference_tuple)
            other_packages = set(other_system.all_packages_dict.values())
            difference = own_packages ^ other_packages

            for differ in difference:
                if differ not in own_packages:
                    current_difference_tuple[0].add(differ)
                else:
                    current_difference_tuple[1].add(differ)

        first_return_tuple = (set.intersection(*[difference_tuple[0] for difference_tuple in differences_tuples]),
                              set.intersection(*[difference_tuple[1] for difference_tuple in differences_tuples]))

        return_list = []

        for difference_tuple in differences_tuples:
            current_tuple = (set(), set())
            return_list.append(current_tuple)

            for installed_package in difference_tuple[0]:
                if installed_package not in first_return_tuple[0]:
                    current_tuple[0].add(installed_package)

            for uninstalled_package in difference_tuple[1]:
                if uninstalled_package not in first_return_tuple[1]:
                    current_tuple[1].add(uninstalled_package)

        return first_return_tuple, return_list

    def validate_and_choose_solution(self, solutions: List[List['Package']],
                                     needed_packages: Sequence['Package']) -> List['Package']:
        """
        Validates solutions and lets the user choose a solution

        :param solutions:           The solutions
        :param needed_packages:     Packages which need to be in the solutions
        :return:                    A chosen and valid solution
        """

        # needed strings
        different_solutions_found = aurman_status("We found {} different valid solutions.\n"
                                                  "You will be shown the differences between the solutions.\n"
                                                  "Choose one of them by entering the corresponding number.\n",
                                                  True, False)
        solution_print = aurman_note("Number {}:\nGetting installed: {}\nGetting removed: {}\n", True, False)
        choice_not_valid = aurman_error("That was not a valid choice!", False, False)

        # calculating new systems and finding valid systems
        new_systems = [self.hypothetical_append_packages_to_system(solution) for solution in solutions]
        valid_systems = []
        valid_solutions_indices = []
        for i, new_system in enumerate(new_systems):
            for package in needed_packages:
                if package.name not in new_system.all_packages_dict:
                    break
            else:
                valid_systems.append(new_system)
                valid_solutions_indices.append(i)

        # no valid solutions
        if not valid_systems:
            logging.error("No valid solutions found")
            raise InvalidInput("No valid solutions found")

        # only one valid solution - just return
        if len(valid_systems) == 1:
            return solutions[valid_solutions_indices[0]]

        # calculate the differences between the resulting systems for the valid solutions
        systems_differences = self.differences_between_systems(valid_systems)

        # if the solutions are different but the resulting systems are not
        single_differences_count = sum(
            [len(diff_tuple[0]) + len(diff_tuple[1]) for diff_tuple in systems_differences[1]])
        if single_differences_count == 0:
            return solutions[valid_solutions_indices[0]]

        # delete duplicate resulting systems
        new_valid_systems = []
        new_valid_solutions_indices = []
        diff_set = set()
        for i, valid_system in enumerate(valid_systems):
            cont_set = frozenset(set.union(systems_differences[1][i][0], systems_differences[1][i][1]))
            if cont_set not in diff_set:
                new_valid_systems.append(valid_system)
                diff_set.add(cont_set)
                new_valid_solutions_indices.append(valid_solutions_indices[i])
        valid_systems = new_valid_systems
        valid_solutions_indices = new_valid_solutions_indices
        systems_differences = self.differences_between_systems(valid_systems)

        # print for the user
        print(different_solutions_found.format(len(valid_systems)))

        while True:
            # print solutions
            for i in range(0, len(valid_systems)):
                installed_names = [package.name for package in systems_differences[1][i][0]]
                removed_names = [package.name for package in systems_differences[1][i][1]]
                installed_names.sort()
                removed_names.sort()

                print(solution_print.format(i + 1,
                                            ", ".join(
                                                [Colors.BOLD(Colors.LIGHT_GREEN(name)) for name in installed_names]),
                                            ", ".join([Colors.BOLD(Colors.RED(name)) for name in removed_names])))

            try:
                user_input = int(input(aurman_question("Enter the number: ", False, False)))
                if 1 <= user_input <= len(valid_systems):
                    return solutions[valid_solutions_indices[user_input - 1]]
            except ValueError:
                print(choice_not_valid)
            else:
                print(choice_not_valid)

    def show_solution_differences_to_user(self, solution: List['Package'], upstream_system: 'System',
                                          noconfirm: bool, deep_search: bool):
        """
        Shows the chosen solution to the user with package upgrades etc.

        :param solution:            The chosen solution
        :param upstream_system:     System containing the known upstream packages
        :param noconfirm:           True if the user does not need to confirm the solution, False otherwise
        :param deep_search:         If deep_search is active
        """

        def repo_of_package(package_name: str) -> str:
            if package_name not in upstream_system.all_packages_dict:
                return Colors.BOLD(Colors.LIGHT_MAGENTA("local/") + package_name)
            package = upstream_system.all_packages_dict[package_name]
            if package.type_of is PossibleTypes.AUR_PACKAGE or package.type_of is PossibleTypes.DEVEL_PACKAGE:
                return Colors.BOLD(Colors.LIGHT_MAGENTA("aur/") + package_name)
            if package.repo is None:
                return Colors.BOLD(Colors.LIGHT_MAGENTA("local/") + package_name)
            else:
                return Colors.BOLD(Colors.LIGHT_MAGENTA("{}/".format(package.repo)) + package_name)

        # needed strings
        package_to_install = aurman_note("The following {} package(s) "
                                         "are getting "
                                         "{}:".format("{}", Colors.BOLD(Colors.LIGHT_CYAN("installed"))), True, False)
        packages_to_uninstall = aurman_note("The following {} package(s) "
                                            "are getting "
                                            "{}:".format("{}", Colors.BOLD(Colors.LIGHT_CYAN("removed"))), True, False)
        packages_to_upgrade = aurman_note("The following {} package(s) "
                                          "are getting "
                                          "{}:".format("{}", Colors.BOLD(Colors.LIGHT_CYAN("updated"))), True, False)
        packages_to_reinstall = aurman_note("The following {} package(s) "
                                            "are getting "
                                            "{}:".format("{}", Colors.BOLD(Colors.LIGHT_CYAN("reinstalled"))), True,
                                            False)
        user_question = "Do you want to continue?"

        new_system = self.hypothetical_append_packages_to_system(solution)
        differences_to_this_system_tuple = self.differences_between_systems((new_system,))[0]

        to_install_names = set([package.name for package in differences_to_this_system_tuple[0]])
        to_uninstall_names = set([package.name for package in differences_to_this_system_tuple[1]])
        to_upgrade_names = to_install_names & to_uninstall_names
        to_install_names -= to_upgrade_names
        to_uninstall_names -= to_upgrade_names
        just_reinstall_names = set(
            [package.name for package in solution if package.name in new_system.all_packages_dict]) - set.union(
            *[to_upgrade_names, to_install_names, to_uninstall_names])

        # colored names + repos
        print_to_install_names = set([repo_of_package(name) for name in to_install_names])
        print_to_uninstall_names = set([repo_of_package(name) for name in to_uninstall_names])
        print_to_upgrade_names = set([repo_of_package(name) for name in to_upgrade_names])
        print_just_reinstall_names = set([repo_of_package(name) for name in just_reinstall_names])

        # calculate some needed values
        max_package_name_length = max([len(name) for name in set(
            print_to_install_names | print_to_uninstall_names | print_to_upgrade_names | print_just_reinstall_names)],
                                      default=0)

        max_left_side_version_length = max([len(self.all_packages_dict[package_name].version) for package_name in
                                            set(to_uninstall_names | to_upgrade_names | just_reinstall_names)],
                                           default=1)

        if to_install_names:
            print(package_to_install.format(len(to_install_names)))
            for package_name in sorted(list(to_install_names)):
                string_to_print = "   {}  {}  ->  {}".format(
                    repo_of_package(package_name).ljust(max_package_name_length),
                    Colors.RED("/").ljust(max_left_side_version_length + 10),
                    Colors.GREEN(new_system.all_packages_dict[package_name].version))

                print(string_to_print)

        if to_uninstall_names:
            print(packages_to_uninstall.format(len(to_uninstall_names)))
            for package_name in sorted(list(to_uninstall_names)):
                string_to_print = "   {}  {}  ->  {}".format(
                    repo_of_package(package_name).ljust(max_package_name_length),
                    Colors.GREEN(self.all_packages_dict[package_name].version).ljust(max_left_side_version_length + 10),
                    Colors.RED("/"))

                print(string_to_print)

        if to_upgrade_names:
            print(packages_to_upgrade.format(len(to_upgrade_names)))
            for package_name in sorted(list(to_upgrade_names)):
                string_to_print = "   {}  {}  ->  {}".format(
                    repo_of_package(package_name).ljust(max_package_name_length),
                    Colors.RED(self.all_packages_dict[package_name].version).ljust(max_left_side_version_length + 10),
                    Colors.GREEN(new_system.all_packages_dict[package_name].version))

                print(string_to_print)

        if just_reinstall_names:
            print(packages_to_reinstall.format(len(just_reinstall_names)))
            for package_name in sorted(list(just_reinstall_names)):
                string_to_print = "   {}  {}  ->  {}".format(
                    repo_of_package(package_name).ljust(max_package_name_length),
                    Colors.LIGHT_MAGENTA(self.all_packages_dict[package_name].version).ljust(
                        max_left_side_version_length + 10),
                    Colors.LIGHT_MAGENTA(new_system.all_packages_dict[package_name].version))

                print(string_to_print)
            if deep_search:
                aurman_note("You are using {}, hence {} "
                            "is active.".format(Colors.BOLD("--deep_search"), Colors.BOLD("--needed")))
                aurman_note("These packages will not actually be reinstalled.")

        if not noconfirm and not ask_user(user_question, True):
            raise InvalidInput()
