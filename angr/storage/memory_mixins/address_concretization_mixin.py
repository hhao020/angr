import claripy

from . import MemoryMixin
from ... import sim_options as options
from ... import concretization_strategies
from ...state_plugins.inspect import BP_BEFORE, BP_AFTER
from ...errors import SimMergeError, SimUnsatError, SimMemoryAddressError, SimMemoryError
from ...storage.memory import DUMMY_SYMBOLIC_READ_VALUE

class MultiwriteAnnotation(claripy.Annotation):
    @property
    def eliminatable(self):
        return False
    @property
    def relocateable(self):
        return True

def _multiwrite_filter(mem, ast): #pylint:disable=unused-argument
    # this is a huge hack, but so is the whole multiwrite crap
    return any(isinstance(a, MultiwriteAnnotation) for a in ast._uneliminatable_annotations)

class AddressConcretizationMixin(MemoryMixin):
    """
    The address concretization mixin allows symbolic reads and writes to be handled sanely by dispatching them as
    a number of conditional concrete reads/writes. It provides a "concretization strategies" interface allowing the
    process of serializing symbolic addresses into concrete ones to be specified.
    """
    def __init__(self, read_strategies=None, write_strategies=None, **kwargs):
        super().__init__(**kwargs)

        self.read_strategies = read_strategies
        self.write_strategies = write_strategies

    def set_state(self, state):
        super().set_state(state)

        if self.state is not None:
            if self.read_strategies is None:
                self._create_default_read_strategies()
            if self.write_strategies is None:
                self._create_default_write_strategies()

    @MemoryMixin.memo
    def copy(self, memo):
        o = super().copy(memo)
        o.read_strategies = list(self.read_strategies)
        o.write_strategies = list(self.write_strategies)
        return o

    def merge(self, others, merge_conditions, common_ancestor=None):
        super().merge(others, merge_conditions, common_ancestor=common_ancestor)
        self.read_strategies = self._merge_strategies(self.read_strategies, *[
            o.read_strategies for o in others
        ])
        self.write_strategies = self._merge_strategies(self.write_strategies, *[
            o.write_strategies for o in others
        ])

    def _create_default_read_strategies(self):
        """
        This function is used to populate `self.read_strategies` if by set-state time none have been provided
        It uses state options to pick defaults.
        """
        self.read_strategies = [ ]
        if options.APPROXIMATE_MEMORY_INDICES in self.state.options:
            # first, we try to resolve the read address by approximation
            self.read_strategies.append(
                concretization_strategies.SimConcretizationStrategyRange(1024, exact=False),
            )

        # then, we try symbolic reads, with a maximum width of a kilobyte
        self.read_strategies.append(
            concretization_strategies.SimConcretizationStrategyRange(1024)
        )

        if options.CONSERVATIVE_READ_STRATEGY not in self.state.options:
            # finally, we concretize to any one solution
            self.read_strategies.append(
                concretization_strategies.SimConcretizationStrategyAny(),
            )

    def _create_default_write_strategies(self):
        """
        This function is used to populate `self.write_strategies` if by set-state time none have been provided.
        It uses state options to pick defaults.
        """
        self.write_strategies = [ ]
        if options.APPROXIMATE_MEMORY_INDICES in self.state.options:
            if options.SYMBOLIC_WRITE_ADDRESSES not in self.state.options:
                # we try to resolve a unique solution by approximation
                self.write_strategies.append(
                    concretization_strategies.SimConcretizationStrategySingle(exact=False),
                )
            else:
                # we try a solution range by approximation
                self.write_strategies.append(
                    concretization_strategies.SimConcretizationStrategyRange(128, exact=False)
                )

        if options.SYMBOLIC_WRITE_ADDRESSES in self.state.options:
            # we try to find a range of values
            self.write_strategies.append(
                concretization_strategies.SimConcretizationStrategyRange(128)
            )
        else:
            # we try to find a range of values, but only for ASTs annotated with the multiwrite annotation
            self.write_strategies.append(concretization_strategies.SimConcretizationStrategyRange(
                128,
                filter=_multiwrite_filter
            ))

        # finally, we just grab the maximum solution
        if options.CONSERVATIVE_WRITE_STRATEGY not in self.state.options:
            self.write_strategies.append(
                concretization_strategies.SimConcretizationStrategyMax()
            )

    @staticmethod
    def _merge_strategies(*strategy_lists):
        """
        Utility function for merging. Does the merge operation on lists of strategies
        """
        if len(set(len(sl) for sl in strategy_lists)) != 1:
            raise SimMergeError("unable to merge memories with amounts of strategies")

        merged_strategies = [ ]
        for strategies in zip(*strategy_lists):
            if len(set(s.__class__ for s in strategies)) != 1:
                raise SimMergeError("unable to merge memories with different types of strategies")

            unique = list(set(strategies))
            if len(unique) > 1:
                unique[0].merge(unique[1:])
            merged_strategies.append(unique[0])
        return merged_strategies

    def _apply_concretization_strategies(self, addr, strategies, action):
        """
        Applies concretization strategies on the address until one of them succeeds.
        """

        # we try all the strategies in order
        for s in strategies:
            # first, we trigger the SimInspect breakpoint and give it a chance to intervene
            e = addr
            self.state._inspect(
                'address_concretization', BP_BEFORE, address_concretization_strategy=s,
                address_concretization_action=action, address_concretization_memory=self,
                address_concretization_expr=e, address_concretization_add_constraints=True
            )
            s = self.state._inspect_getattr('address_concretization_strategy', s)
            e = self.state._inspect_getattr('address_concretization_expr', addr)

            # if the breakpoint None'd out the strategy, we skip it
            if s is None:
                continue

            # let's try to apply it!
            try:
                a = s.concretize(self, e)
            except SimUnsatError:
                a = None

            # trigger the AFTER breakpoint and give it a chance to intervene
            self.state._inspect(
                'address_concretization', BP_AFTER,
                address_concretization_result=a
            )
            a = self.state._inspect_getattr('address_concretization_result', a)

            # return the result if not None!
            if a is not None:
                return a

        # well, we tried
        raise SimMemoryAddressError(
            "Unable to concretize address for %s with the provided strategies." % action
        )

    def concretize_write_addr(self, addr, strategies=None):
        """
        Concretizes an address meant for writing.

        :param addr:            An expression for the address.
        :param strategies:      A list of concretization strategies (to override the default).
        :returns:               A list of concrete addresses.
        """

        if isinstance(addr, int):
            return [ addr ]
        elif not self.state.solver.symbolic(addr):
            return [ self.state.solver.eval(addr) ]

        strategies = self.write_strategies if strategies is None else strategies
        return self._apply_concretization_strategies(addr, strategies, 'store')

    def concretize_read_addr(self, addr, strategies=None):
        """
        Concretizes an address meant for reading.

        :param addr:            An expression for the address.
        :param strategies:      A list of concretization strategies (to override the default).
        :returns:               A list of concrete addresses.
        """

        if isinstance(addr, int):
            return [ addr ]
        elif not self.state.solver.symbolic(addr):
            return [ self.state.solver.eval(addr) ]

        strategies = self.read_strategies if strategies is None else strategies
        return self._apply_concretization_strategies(addr, strategies, 'load')

    #
    # Real shit
    #


    def load(self, addr, size=None, condition=None, **kwargs):
        if type(size) is not int:
            raise TypeError("Size must have been specified as an int before reaching address concretization")

        try:
            concrete_addrs = self.concretize_read_addr(addr)
        except SimMemoryError:
            if options.CONSERVATIVE_READ_STRATEGY in self.state.options:
                return self._default_value(None, size, name='symbolic_read_unconstrained', **kwargs)
            else:
                raise

        # quick optimization so as to not involve the solver if not necessary
        trivial = len(concrete_addrs) == 1 and (addr == concrete_addrs[0]).is_true()
        if not trivial:
            # apply the concretization results to the state
            constraint_options = [addr == concrete_addr for concrete_addr in concrete_addrs]
            conditional_constraint = self.state.solver.Or(*constraint_options)
            self._add_constraints(conditional_constraint, condition=condition, **kwargs)

        read_value = DUMMY_SYMBOLIC_READ_VALUE  # this is a sentinel value and should never be touched

        for concrete_addr in concrete_addrs:
            # perform each of the loads
            # the implementation of the "fallback" value ought to be implemented above this in the stack!!
            if trivial:
                sub_condition = condition
            else:
                sub_condition = addr == concrete_addr
                if condition is not None:
                    sub_condition = condition & sub_condition

            sub_value = super().load(concrete_addr, size=size, condition=sub_condition, **kwargs)

            # quick optimization to not introduce the DUMMY value if there's only one loop
            if len(concrete_addrs) == 1:
                read_value = sub_value
            else:
                read_value = self.state.solver.If(addr == concrete_addr, sub_value, read_value)

        return read_value

    def store(self, addr, data, size=None, condition=None, **kwargs):
        try:
            concrete_addrs = sorted(self.concretize_write_addr(addr))
        except SimMemoryError:
            if options.CONSERVATIVE_WRITE_STRATEGY in self.state.options:
                return  # not completed
            else:
                raise

        # quick optimization so as to not involve the solver if not necessary
        trivial = len(concrete_addrs) == 1 and (addr == concrete_addrs[0]).is_true()
        if not trivial:
            # apply the concretization results to the state
            constraint_options = [addr == concrete_addr for concrete_addr in concrete_addrs]
            conditional_constraint = self.state.solver.Or(*constraint_options)
            self._add_constraints(conditional_constraint, condition=condition, **kwargs)

        for concrete_addr in concrete_addrs:
            # perform each of the stores as conditional
            # the implementation of conditionality must be at the bottom of the stack
            if trivial:
                sub_condition = condition
            else:
                sub_condition = addr == concrete_addr
                if condition is not None:
                    sub_condition = condition & sub_condition
            super().store(concrete_addr, data, size=size, condition=sub_condition, **kwargs)
