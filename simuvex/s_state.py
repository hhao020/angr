#!/usr/bin/env python

import copy

import symexec
import s_memory
import s_arch
import hashlib
import json

import logging
l = logging.getLogger("s_state")

class SimState:
	def __init__(self, temps=None, registers=None, memory=None, old_constraints=None, id="", arch="AMD64", block_path=None, memory_backer={ }):
		self.temps = temps if temps else { }
		self.memory = memory if memory else s_memory.SimMemory(id="mem", backer=memory_backer)
		# self.registers = registers if registers else { }
		self.registers = registers if registers else s_memory.SimMemory(id="reg") ## this is because vex treats registers as memory
		self.old_constraints = old_constraints if old_constraints else [ ]
		self.block_path = block_path if block_path else [ ]
		self.new_constraints = [ ]
		self.branch_constraints = [ ]
		self.id = id
		self.arch = s_arch.Architectures[arch] if isinstance(arch, str) else arch

		try:
			self.id = "0x%x" % int(str(self.id))
		except:
			pass

	def constraints_after(self):
		return self.old_constraints + self.new_constraints + self.branch_constraints

	def constraints_before(self):
		return copy.copy(self.old_constraints)

	def constraints_avoid(self):
		return self.old_constraints + [ symexec.Not(symexec.And(*self.branch_constraints)) ]

	def add_constraints(self, *args):
		self.new_constraints.extend(args)

	def add_branch_constraints(self, *args):
		self.branch_constraints.extend(args)

	def inplace_after(self):
		self.old_constraints = self.constraints_after()
		self.new_constraints = [ ]
		self.branch_constraints = [ ]

	def clear_constraints(self):
		self.old_constraints = [ ]
		self.new_constraints = [ ]
		self.branch_constraints = [ ]

	def copy_unconstrained(self):
		c_temps = self.temps
		c_mem = self.memory.copy()
		c_registers = self.registers.copy()
		c_constraints = [ ]
		c_id = self.id
		c_arch = self.arch
		c_bs = copy.copy(self.block_path)

		return SimState(c_temps, c_registers, c_mem, c_constraints, c_id, c_arch, c_bs)


	def copy_after(self):
		c = self.copy_unconstrained()
		c.old_constraints = self.constraints_after()
		return c

	def copy_before(self):
		c = self.copy_unconstrained()
		c.old_constraints = self.constraints_before()

		return c

	def copy_avoid(self):
		c = self.copy_unconstrained()
		c.old_constraints = self.constraints_avoid()
		return c

	def copy_exact(self):
		c = self.copy_before(self)
		c.new_constraints = copy.copy(self.new_constraints)
		c.branch_constraints = copy.copy(self.branch_constraints)

        #FIXME: block_path.
        def hash_cnt(self):
                constr = self.constraints_after()
                m = hashlib.sha256()
                m.update(json.dumps([[n, str(cnt)] for n, cnt in  self.temps.iteritems()]))
                m.update(self.memory.to_json())
                m.update(self.registers.to_json())
                m.update(json.dumps([str(cnt) for cnt in constr]))
                m.update(self.arch.vex_arch)
                return m.digest()
