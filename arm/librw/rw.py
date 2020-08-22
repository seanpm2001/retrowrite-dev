import argparse
from collections import defaultdict

from capstone import CS_OP_IMM, CS_GRP_JUMP, CS_GRP_CALL, CS_OP_MEM, CS_OP_REG
from capstone.x86_const import X86_REG_RIP

from elftools.elf.descriptions import describe_reloc_type
from elftools.elf.enums import ENUM_RELOC_TYPE_x64
from elftools.elf.enums import ENUM_RELOC_TYPE_AARCH64

from arm.librw.util.logging import *
from arm.librw.util.arm_util import _is_jump_conditional, is_reg_32bits, get_64bits_reg
from arm.librw.container import InstrumentedInstruction
from arm.librw.emulation import Path, Expr

class Rewriter():
    GCC_FUNCTIONS = [ # functions added by the compiler. No use in rewriting them
        "_start",
        "__libc_start_main",
        "__libc_csu_fini",
        "__libc_csu_init",
        "__lib_csu_fini",
        "_init",
        "__libc_init_first",
        "_fini",
        "_rtld_fini",
        "_exit",
        "__get_pc_think_bx",
        "__do_global_dtors_aux",
        "__gmon_start",
        "frame_dummy",
        "__do_global_ctors_aux",
        "__register_frame_info",
        "deregister_tm_clones",
        "register_tm_clones",
        "__do_global_dtors_aux",
        "__frame_dummy_init_array_entry",
        "__init_array_start",
        "__do_global_dtors_aux_fini_array_entry",
        "__init_array_end",
        "__stack_chk_fail",
        "__cxa_atexit",
        "__cxa_finalize",
        "call_weak_fn" #not really sure about this, but imho we should'nt touch it
    ]

    GCC_RELOCATIONS = [ # relocations added by the compiler. Do not touch em!
        "__gmon_start__",
        "_ITM_deregisterTMCloneTable",
        "_ITM_registerTMCloneTable"
    ]


    # DATASECTIONS = [".rodata", ".data", ".bss", ".data.rel.ro", ".init_array"]
    # DATASECTIONS = [".got", ".fini_array",  ".rodata", ".data", ".bss", ".data.rel.ro", ".init_array"]
    DATASECTIONS = [".got", ".rodata", ".data", ".bss", ".data.rel.ro", ".init_array"]

    def __init__(self, container, outfile):
        #XXX: remove global
        self.container = container
        self.outfile = outfile

        for sec, section in self.container.sections.items():
            section.load()

        for _, function in self.container.functions.items():
            if function.name in Rewriter.GCC_FUNCTIONS:
                container.ignore_function_addrs += [function.start]
                continue
            function.disasm()

    def symbolize(self):
        symb = Symbolizer()
        symb.symbolize_text_section(self.container, None)
        symb.symbolize_data_sections(self.container, None)

    def dump(self):
        results = list()

        for sec, section in sorted(
                self.container.sections.items(), key=lambda x: x[1].base):
            results.append("%s" % (section))

        results.append(".section .text")
        results.append(".align 16")

        for _, function in sorted(self.container.functions.items()):
            if function.name in Rewriter.GCC_FUNCTIONS:
                continue
            results.append("\t.text\n%s" % (function))

        with open(self.outfile, 'w') as outfd:
            outfd.write("\n".join(results + ['']))

        
        fixed_sections = [".bss", ".data", ".data.rel.ro", ".rodata", ".text"]
        seclist = "-Wl"
        for sec, section in sorted(
                self.container.sections.items(), key=lambda x: x[1].base):
            if section not in fixed_sections: continue
            seclist += f",--section-start={section.name}={hex(section.base)}"
        info(f"gcc cmdline flag: {seclist}")
        info(f"Success: retrowritten assembly to {self.outfile}")


class Symbolizer():
    def __init__(self):
        self.bases = set()
        self.xrefs = defaultdict(list)
        self.symbolized = set()

    # TODO: Use named symbols instead of generic labels when possible.
    # TODO: Replace generic call labels with function names instead
    def symbolize_text_section(self, container, context):
        # Symbolize using relocation information.
        for rel in container.relocations[".text"]:
            info("INSTRUCTION NOT FOUND")
            fn = container.function_of_address(rel['offset'])
            if not fn or fn.name in Rewriter.GCC_FUNCTIONS:
                continue

            inst = fn.instruction_of_address(rel['offset'])
            if not inst:
                continue

            # Fix up imports
            if "@" in rel['name']:
                suffix = ""
                if rel['st_value'] == 0:
                    suffix = "@PLT"

                # XXX: ARM
                if len(inst.cs.operands) == 1:
                    inst.op_str = "%s%s" % (rel['name'].split("@")[0], suffix)
                else:
                    # Figure out which argument needs to be
                    # converted to a symbol.
                    if suffix:
                        suffix = "@PLT"
                    mem_access, _ = inst.get_mem_access_op()
                    if not mem_access:
                        continue
                    value = hex(mem_access.disp)
                    inst.op_str = inst.op_str.replace(
                        value, "%s%s" % (rel['name'].split("@")[0], suffix))
            else:
                mem_access, _ = inst.get_mem_access_op()
                if not mem_access:
                    # These are probably calls?
                    continue

                # XXX: ARM
                if (rel['type'] in [
                        ENUM_RELOC_TYPE_x64["R_X86_64_PLT32"],
                        ENUM_RELOC_TYPE_x64["R_X86_64_PC32"]
                ]):

                    value = mem_access.disp
                    ripbase = inst.address + inst.sz
                    inst.op_str = inst.op_str.replace(
                        hex(value), ".LC%x" % (ripbase + value))
                    if ".rodata" in rel["name"]:
                        self.bases.add(ripbase + value)
                else:
                    print("[*] Possible incorrect handling of relocation!")
                    value = mem_access.disp
                    inst.op_str = inst.op_str.replace(
                        hex(value), ".LC%x" % (rel['st_value']))

            self.symbolized.add(inst.address)

        self.symbolize_cf_transfer(container, context)
        self.reverse_nexts(container)
        # Symbolize remaining memory accesses
        self.symbolize_switch_tables(container, context)
        self.symbolize_mem_accesses(container, context)



    def symbolize_cf_transfer(self, container, context=None):
        for _, function in container.functions.items():
            function.addr_to_idx = dict()
            for inst_idx, instruction in enumerate(function.cache):
                function.addr_to_idx[instruction.address] = inst_idx

            for inst_idx, instruction in enumerate(function.cache):
                is_jmp = CS_GRP_JUMP in instruction.cs.groups
                is_call = "bl" in instruction.cs.mnemonic

                if not (is_jmp or is_call):
                    # Simple, next is idx + 1
                    # XXX: ARM
                    if instruction.mnemonic.startswith('ret'):
                        function.nexts[inst_idx].append("ret")
                        instruction.cf_leaves_fn = True
                    else:
                        function.nexts[inst_idx].append(inst_idx + 1)
                    continue

                instruction.cf_leaves_fn = False

                if is_jmp and _is_jump_conditional(instruction.mnemonic):
                    if inst_idx + 1 < len(function.cache):
                        # Add natural flow edge
                        function.nexts[inst_idx].append(inst_idx + 1)
                    else:
                        # Out of function bounds, no idea what to do!
                        function.nexts[inst_idx].append("undef")
                elif is_call:
                    instruction.cf_leaves_fn = True
                    function.nexts[inst_idx].append("call")
                    if inst_idx + 1 < len(function.cache):
                        function.nexts[inst_idx].append(inst_idx + 1)
                    else:
                        # Out of function bounds, no idea what to do!
                        function.nexts[inst_idx].append("undef")

                target = 0
                if instruction.cs.operands[-1].type == CS_OP_IMM: # b 0xf20
                    target = instruction.cs.operands[-1].imm
                elif instruction.cs.operands[-1].type == CS_OP_REG: # br x0
                    function.switches += [instruction.address]
                if target:
                    # Check if the target is in .text section.
                    if container.is_in_section(".text", target):
                        function.bbstarts.add(target)
                        instruction.op_str = instruction.op_str.replace("#0x%x" % target, ".LC%x" % target)
                    elif target in container.plt:
                        instruction.op_str = "{}".format(
                            container.plt[target])
                    else:
                        gotent = container.is_target_gotplt(target)
                        if gotent:
                            found = False
                            for relocation in container.relocations[".dyn"]:
                                if gotent == relocation['offset']:
                                    instruction.op_str = "{}@PLT".format(
                                        relocation['name'])
                                    found = True
                                    break
                            if not found:
                                print("[x] Missed GOT entry!")
                        else:
                            print("[x] Missed call target: %x" % (target))

                    if is_jmp:
                        if target in function.addr_to_idx:
                            idx = function.addr_to_idx[target]
                            function.nexts[inst_idx].append(idx)
                        else:
                            instruction.cf_leaves_fn = True
                            function.nexts[inst_idx].append("undef")
                elif is_jmp:
                    function.nexts[inst_idx].append("undef")

    def reverse_nexts(self, container):
        for _, function in container.functions.items():
            function.prevs = {}
            for idx, nexts in function.nexts.items():
                for nexti in nexts:
                    function.prevs[nexti] = function.prevs.get(nexti, [])
                    function.prevs[nexti].append(idx)

    def resolve_register_value(self, register, function, instr):
        debug(f"Instructions leading up to {hex(instr.address)}")
        inst_idx = function.addr_to_idx[instr.address]
        reg_name = instr.cs.reg_name(register)
        visited = [False]*len(function.cache)
        paths = [Path(function, inst_idx, reg_pool=[reg_name], exprvalue=f"{reg_name}")]
        paths_finished = []
        while len(paths) > 0:
            p = paths[0]
            if inst_idx == 0 or visited[inst_idx] or len(p.reg_pool) == 0:  # we got to the start of the function
                paths_finished += [paths[0]]
                del paths[0]
                continue

            visited[inst_idx] = True
            prevs = function.prevs[inst_idx]
            if len(prevs) > 1:
                print("MULTIPLE prevs: ", prevs)
            inst_idx = prevs[0]
            instr = function.cache[inst_idx]
            regs_write = instr.reg_writes_common()
            if any([reg in p.reg_pool for reg in regs_write]):
                debug(f"next: {instr.cs}")
                p.emulate(instr)
                debug(f"step: {instr.cs} - expr: {p.expr}")

        for p in paths_finished:
            p.expr.simplify()
            debug("FINAL " + str(p.expr))
            return p.expr

    def _memory_read(self, container, addr, size, signed=False):
        sec = container.section_of_address(addr)
        if sec.name != ".rodata": 
            debug(f"WARNING: changing value not in rodata but in {sec.name}")
        return sec.read_at(addr, size, signed)

    def _memory_replace(self, container, addr, size, value):
        sec = container.section_of_address(addr)
        if sec.name != ".rodata": 
            debug(f"WARNING: changing value not in rodata but in {sec.name}")
        sec.replace(addr, size, value)

    def _guess_cases_number(self, container, function, addr):
        max_instrs_before_switch = 20
        inst_idx = function.addr_to_idx[addr]
        instr = function.cache[inst_idx]
        found = -1
        reg_index = False
        class path:
            def __init__(self, steps=0, reg_index=False, found=-1, idx=inst_idx):
                self.steps = steps
                self.reg_index = reg_index
                self.found = found
                self.inst_idx = idx

        paths = [path()] 
        paths_finished = []
        while len(paths):
            p = paths[0]
            p.steps += 1
            if inst_idx == 0 or p.steps > max_instrs_before_switch:
                paths_finished += [paths.pop(0)]
                continue

            prevs = function.prevs[p.inst_idx]
            p.inst_idx = prevs[0]
            for prev in prevs[1:]:
                paths += [path(p.steps, p.reg_index, p.found, prev)]
            instr = function.cache[p.inst_idx]

            if instr.mnemonic.startswith("ldr") and not p.reg_index:
                mem, mem_op_idx = instr.get_mem_access_op()
                if mem.index: p.reg_index = mem.index
            if instr.mnemonic.startswith("cmp") and p.reg_index:
                if instr.cs.operands[0].reg == p.reg_index and \
                   instr.cs.operands[1].type == CS_OP_IMM:
                    p.found = instr.cs.operands[1].imm
                    paths_finished += [paths.pop(0)]
                    continue
            if instr.mnemonic.startswith("movz") and instr.cs.operands[0].reg == p.reg_index:
                # if the register is written with a constant value, we just ignore this path
                paths.pop(0)
                continue
        debug(f"number of cases: {[p.found for p in paths_finished]}")
        if any([paths_finished[i].found != paths_finished[i+1].found 
            for i in range(len(paths_finished)-1)]):
            return -1 # if there are inconsistencies, we quit!
        return paths_finished[0].found

    def symbolize_switch_tables(self, container, context):
        rodata = container.sections.get(".rodata", None)
        if not rodata:
            assert False
        for _, function in container.functions.items():
            for jump in function.switches:
                inst_idx = function.addr_to_idx[jump]
                instr = function.cache[inst_idx]
                reg = instr.cs.operands[0].reg
                debug(f"Analyzing switch on {instr.cs}, {instr.cs.reg_name(reg)}")
                expr = self.resolve_register_value(reg, function, instr)

                # Super advanced pattern matching
                # import IPython; IPython.embed() 

                # addr
                if isinstance(expr.left, int):
                    debug(f"Found a blr at {instr.cs} but it is really a call to {hex(expr.left)}! - ignoring for now")
                    continue

                # x0
                if isinstance(expr.left, str):
                    continue

                # [addr]
                elif expr.left.mem and expr.left.right == None: 
                    # addr = int(str(expr.left.left))
                    # value = self._memory_read(container, addr, 8)
                    # swlbl = ".LC%x" % (value,)
                    # self._memory_replace(container, addr, 8, swlbl)
                    continue

                # first_case_addr + [ jmptbl_addr + ??? ]
                elif isinstance(expr.left.right, Expr) and expr.left.right.left \
                    and expr.left.right.left.mem and isinstance(expr.left.right.left.left, int):
                    base_case = expr.left.left
                    debug(f"BASE CASE: {base_case}")
                    size = expr.left.right.left.mem
                    jmptbl = int(str(expr.left.right.left.left))
                    shift = expr.left.right.right
                    debug(f"SHIFT: {shift}")
                    debug(f"JMPTBL: {jmptbl}")
                    debug(f"SIZE: {size}")

                    cases = self._guess_cases_number(container, function, instr.address)
                    debug(f"CASES: {cases}")
                    if cases == -1:
                        assert False

                    for i in range(cases):
                        value = self._memory_read(container, jmptbl + i*size, size, signed=True)
                        debug("VALUE:" + str(value))
                        addr = base_case + value*(2**shift)
                        swlbl = "(.LC%x-.LC%x)/%d" % (addr, base_case, 2**shift)
                        self._memory_replace(container, jmptbl + i*size, size, swlbl)

                else:
                    critical(f"JUMP TABLE at {hex(instr.address)} impossible to recognize!")
                    critical(f"Potential crashes in function {function.name}")
                    critical(f"final expression: {expr}")



    def _adjust_target(self, container, target):
        # Find the nearest section
        sec = None
        for sname, sval in sorted(
                container.sections.items(), key=lambda x: x[1].base):
            if sval.base >= target:
                break
            sec = sval

        assert sec is not None

        end = sec.base  # + sec.sz - 1
        adjust = target - end

        assert adjust > 0

        return end, adjust

    def _is_target_in_region(self, container, target):
        for sec, sval in container.sections.items():
            if sval.base <= target < sval.base + sval.sz:
                return True

        for fn, fval in container.functions.items():
            if fval.start <= target < fval.start + fval.sz:
                return True

        return False

    def _adjust_adrp_section_pointer(self, container, secname, orig_off, instruction):
        # we adjust things like adrp x0, 0x10000 (start of .bss)
        # to stuff like  ldr x0, =(.bss - (offset))
        # to make global variables work
        assert instruction.mnemonic == "adrp"
        base = container.sections[secname].base
        reg_name = instruction.reg_writes()[0]
        diff = base - orig_off
        op = '-' if diff > 0 else '+'
        if secname == ".got": secname = ".fake_got" # the got is special, as it
        # will get overwritten by the compiler after reassembly. We introduce a 
        # "fake_got" label so that we keep track of where the "old" got section was
        instruction.mnemonic = "ldr"
        instruction.op_str = "%s, =(%s %c 0x%x)"  % (reg_name, secname, op, abs(diff))


    def _adjust_global_access(self, container, function, edx, inst):
        # there is an adrp somewhere in the code, this means global variable...
        # global variable addresses are dynamically built in multiple instructions
        # here we try to resolve the address with some capstone trickery and shady assumptions

        orig_off = inst.cs.operands[1].imm
        orig_reg = inst.reg_writes()[0]

        possible_sections = []
        for name,s in container.sections.items():
            if s.base // 0x1000 == orig_off // 0x1000 or \
               s.base <= orig_off < s.base + s.sz:
                possible_sections += [name]

        if len(possible_sections) == 1:
            secname = possible_sections[0]
            if secname not in [".text"]:  # .text can get instrumented, we need to know the exact address
                self._adjust_adrp_section_pointer(container, secname, orig_off, inst)
                return

        debug(f"Global access at {inst}, multiple sections possible: {possible_sections}, trying to resolve address...")

        # if we got here, it's bad, as we're not sure which section the global variable is in.
        # We try to resolve all possible addresses by simulating all possible control flow paths.

        to_fix = []
        visited = [0]*function.sz
        from collections import deque
        paths = deque(function.nexts[edx])
        backs = {}
        b = 0
        while len(paths):
            idx = paths.pop()
            if not isinstance(idx, int): continue
            if visited[idx]: continue
            visited[idx] = 1
            inst2 = function.cache[idx]
            # if inst.address == 0xd7df4:
                # print("visiting", inst2)
            # if inst.address == 0x59a40:
                # print(inst.cs, orig_reg)
                # print(inst2.cs)
            if orig_reg in inst2.reg_reads():
                if '=' in inst2.op_str:  # ugly hack to avoid reinstrumenting instructions
                    continue
                if "str" in inst.mnemonic and 
                to_fix += [inst2]
            if orig_reg in inst2.reg_writes_common():
                continue  # we overwrote the register we're trying to resolve, so abandon this path
            for n in function.nexts[idx]:
                if not isinstance(n, int): continue
                if n == 0: continue # avoid recursive calls 
                # if n < idx: continue
                if not visited[n]: 
                    backs[n] = idx
                paths.append(n)


        resolved_addresses = []
        for inst2 in to_fix:
            if inst2.mnemonic == "add":
                assert inst2.cs.operands[2].type == CS_OP_IMM
                assert all([op.shift.value == 0 for op in inst2.cs.operands])
                resolved_addresses += [(inst2, orig_off + inst2.cs.operands[2].imm)]
            elif inst2.mnemonic.startswith("ldr"):
                assert inst2.cs.operands[1].type == CS_OP_MEM
                if not all([op.shift.value == 0 for op in inst2.cs.operands]):
                    continue
                resolved_addresses += [(inst2, orig_off + inst2.cs.operands[1].mem.disp)]
            elif inst2.mnemonic.startswith("str"):
                if inst2.cs.reg_name(inst2.cs.operands[0].reg) == orig_reg: # str <orig_reg>, [...]
                    resolved_addresses += [(inst2, orig_off)]
                else: # str ..., [<orig_reg> + ...]
                    resolved_addresses += [(inst2, orig_off + inst2.cs.operands[1].mem.disp)]
            else:
                continue


        possible_sections = set()
        for r in resolved_addresses[:]:
            (i, addr) = r[0], r[1]
            sec = container.section_of_address(addr)
            if sec:
                debug(f"to_fix: {i.cs}, in section {sec.name}")
                possible_sections.add(sec.name)
            else:
                critical(f"WARNING: no section for resolved addr {hex(addr)} at {i.cs}, ignoring")
                resolved_addresses.remove(r)

        if inst.address == 0x4d41c:
            print(inst2.cs)
            print([f.cs for f in to_fix])
            exit(1)

        debug(f"After resolving addresses, here are the possible sections: {possible_sections}")
        if len(possible_sections) == 1:
            secname = list(possible_sections)[0]
            if secname not in [".text"]:
                self._adjust_adrp_section_pointer(container, secname, orig_off, inst)
                debug(f"We're good, false alarm, the only possible section is: {secname}. Nice!")
                return


        # if we got here, it's *very* bad, as we're *still* not sure which section the global variable is in,
        # or that section is the .text section. 
        # As our last hope we try to ugly-hack-patch all instructions that use the register with the
        # global address loaded (instructions in the to_fix list)

        debug(f"WARNING: trying to fix each instruction manually...")


        # we don't really want an ADRP in the middle of code, if possible
        # so we erase it
        inst.mnemonic = ""
        inst.op_str = ""

        for inst2, resolved_address in resolved_addresses:

            if inst2.mnemonic not in ["add", "ldr"] and not inst2.mnemonic.startswith("str"):
                debug(f"Warning: skipping {inst2} as not supported in global address emulation")
                continue

            dereference_resolved = True
            if inst2.mnemonic == "add":
                inst2.mnemonic = "ldr"
                dereference_resolved = False

            is_an_import = False
            for rel in container.relocations[".dyn"]:
                if rel['st_value'] == resolved_address or rel['offset'] == resolved_address:
                    is_an_import = rel['name']
                    break
                elif resolved_address in container.plt:
                    is_an_import = container.plt[resolved_address]
                    break

            reg_name2 = inst2.cs.reg_name(inst2.cs.operands[0].reg)
            reg_name3 = inst2.cs.reg_name(inst2.cs.operands[1].reg)

            if inst2.mnemonic.startswith("str"):
                old_mnemonic = inst2.mnemonic
                if reg_name2 == orig_reg:  # str <orig_reg>, [...]
                    inst2.instrument_before(InstrumentedInstruction(
                        f"ldr {reg_name2}, =.LC%x" % (resolved_address)))
                else: # str ..., [<orig_reg> + ...]
                    inst2.mnemonic =  "ldr"
                    inst2.op_str =  reg_name3 + f", =.LC%x" % (resolved_address)
                    inst2.instrument_after(InstrumentedInstruction(
                        f"{old_mnemonic} {reg_name2}, [{reg_name3}]"))
            elif is_an_import:
                inst2.op_str =  reg_name2 + f", =%s" % (is_an_import)
            else:
                if is_reg_32bits(reg_name2): # because of a gcc bug? we cannot have ldr w0, =.label, only x0
                    reg_name2 = get_64bits_reg(reg_name2)
                inst2.op_str =  reg_name2 + f", =.LC%x" % (resolved_address)
                if dereference_resolved:
                    if reg_name2.startswith("w"): reg_name2 = 'x' + reg_name2[1:] #XXX: fix this ugly hack
                    inst2.instrument_after(InstrumentedInstruction(
                        f"ldr {reg_name2}, [{reg_name2}]"))

            inst2.op_str += " // from adrp at 0x%x" % (inst.address)

            self.xrefs[resolved_address] += [inst2.address]






    def symbolize_mem_accesses(self, container, context):
        for _, function in container.functions.items():
            for edx, inst in enumerate(function.cache):
                if inst.address in self.symbolized:
                    continue

                if inst.mnemonic == "adrp":
                    self._adjust_global_access(container, function, edx, inst)

                if inst.mnemonic == "adr":
                    inst.op_str = inst.op_str.replace("#0x%x" % inst.cs.operands[1].imm, ".LC%x" % inst.cs.operands[1].imm)


                mem_access, _ = inst.get_mem_access_op()
                if not mem_access:
                    continue



                # Now we have a memory access,
                # check if it is rip relative.
                base = mem_access.base
                # XXX: ARM
                if base == X86_REG_RIP:
                    debug(f"INSTRUCTION CHANGED FROM {inst}  ", end="")
                    value = mem_access.disp
                    ripbase = inst.address + inst.sz
                    target = ripbase + value

                    is_an_import = False

                    for relocation in container.relocations[".dyn"]:
                        if relocation['st_value'] == target:
                            is_an_import = relocation['name']
                            sfx = ""
                            break
                        elif target in container.plt:
                            is_an_import = container.plt[target]
                            sfx = "@PLT"
                            break
                        elif relocation['offset'] == target:
                            is_an_import = relocation['name']
                            sfx = "@GOTPCREL"
                            break

                    if is_an_import:
                        inst.op_str = inst.op_str.replace(
                            hex(value), "%s%s" % (is_an_import, sfx))
                    else:
                        # Check if target is contained within a known region
                        in_region = self._is_target_in_region(
                            container, target)
                        if in_region:
                            inst.op_str = inst.op_str.replace(
                                hex(value), ".LC%x" % (target))
                        else:
                            target, adjust = self._adjust_target(
                                container, target)
                            inst.op_str = inst.op_str.replace(
                                hex(value), "%d+.LC%x" % (adjust, target))
                            print("[*] Adjusted: %x -- %d+.LC%x" %
                                  (inst.address, adjust, target))

                    debug(f"TO:   {inst}")

    def _handle_relocation(self, container, section, rel):
        reloc_type = rel['type']
        # elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_RELATIVE"]:
        if reloc_type == ENUM_RELOC_TYPE_AARCH64["R_AARCH64_RELATIVE"]:
            value = rel['addend']
            label = ".LC%x" % value
            if int(value) in container.ignore_function_addrs:
                return
            section.replace(rel['offset'], 8, label)
        elif reloc_type == ENUM_RELOC_TYPE_AARCH64["R_AARCH64_GLOB_DAT"]:
            if section.name != '.got': return
            if rel['name'] in Rewriter.GCC_RELOCATIONS: return
            section.replace(rel['offset'], 8, rel['name'])

        else:
            print(rel)
            print("[*] Unhandled relocation {}".format(
                describe_reloc_type(reloc_type, container.loader.elffile)))

    def symbolize_data_sections(self, container, context=None):
        # Section specific relocation
        for secname, section in container.sections.items():
            for rel in section.relocations:
                self._handle_relocation(container, section, rel)

        # .dyn relocations
        dyn = container.relocations[".dyn"]
        for rel in dyn:
            section = container.section_of_address(rel['offset'])
            if section:
                self._handle_relocation(container, section, rel)
            else:
                print("[x] Couldn't find valid section {:x}".format(
                    rel['offset']))


if __name__ == "__main__":
    from .loader import Loader
    from .analysis import register

    argp = argparse.ArgumentParser()

    argp.add_argument("bin", type=str, help="Input binary to load")
    argp.add_argument("outfile", type=str, help="Symbolized ASM output")

    args = argp.parse_args()

    loader = Loader(args.bin)

    flist = loader.flist_from_symtab()
    loader.load_functions(flist)

    slist = loader.slist_from_symtab()
    loader.load_data_sections(slist, lambda x: x in Rewriter.DATASECTIONS)

    reloc_list = loader.reloc_list_from_symtab()
    loader.load_relocations(reloc_list)

    global_list = loader.global_data_list_from_symtab()
    loader.load_globals_from_glist(global_list)

    loader.container.attach_loader(loader)

    rw = Rewriter(loader.container, args.outfile)
    rw.symbolize()
    rw.dump()
