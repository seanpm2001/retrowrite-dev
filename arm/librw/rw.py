import argparse
from collections import defaultdict

from capstone import CS_OP_IMM, CS_GRP_JUMP, CS_GRP_CALL, CS_OP_MEM, CS_OP_REG
from capstone.x86_const import X86_REG_RIP

from elftools.elf.descriptions import describe_reloc_type
from elftools.elf.enums import ENUM_RELOC_TYPE_x64
from elftools.elf.enums import ENUM_RELOC_TYPE_AARCH64

from arm.librw.util.logging import *
from arm.librw.util.arm_util import _is_jump_conditional
from arm.librw.container import InstrumentedInstruction


class Rewriter():
    GCC_FUNCTIONS = [
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


class Symbolizer():
    def __init__(self):
        self.bases = set()
        self.pot_sw_bases = defaultdict(set)
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
                        self.pot_sw_bases[fn.start].add(ripbase + value)
                else:
                    print("[*] Possible incorrect handling of relocation!")
                    value = mem_access.disp
                    inst.op_str = inst.op_str.replace(
                        hex(value), ".LC%x" % (rel['st_value']))

            self.symbolized.add(inst.address)

        self.symbolize_cf_transfer(container, context)
        # Symbolize remaining memory accesses
        self.symbolize_mem_accesses(container, context)
        self.symbolize_switch_tables(container, context)


    def symbolize_cf_transfer(self, container, context=None):
        for _, function in container.functions.items():
            addr_to_idx = dict()
            for inst_idx, instruction in enumerate(function.cache):
                addr_to_idx[instruction.address] = inst_idx

            for inst_idx, instruction in enumerate(function.cache):
                # XXX: ARM
                # ARM64_GRP_CALL
                is_jmp = CS_GRP_JUMP in instruction.cs.groups
                is_call = CS_GRP_CALL in instruction.cs.groups

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

                # XXX: Capstone does not recognize "bl" (branch and link) as a call
                if is_jmp and _is_jump_conditional(instruction.mnemonic):
                    if inst_idx + 1 < len(function.cache):
                        # Add natural flow edge
                        function.nexts[inst_idx].append(inst_idx + 1)
                    else:
                        # Out of function bounds, no idea what to do!
                        function.nexts[inst_idx].append("undef")
                elif is_call:
                    instruction.cf_leaves_fn = True
                    # XXX: ARM
                    function.nexts[inst_idx].append("call")
                    if inst_idx + 1 < len(function.cache):
                        function.nexts[inst_idx].append(inst_idx + 1)
                    else:
                        # Out of function bounds, no idea what to do!
                        function.nexts[inst_idx].append("undef")

                # XXX: what if we jump on a register value i.e. we don't know the target?
                if instruction.cs.operands[0].type == CS_OP_IMM:
                    target = instruction.cs.operands[0].imm
                    # Check if the target is in .text section.
                    if container.is_in_section(".text", target):
                        function.bbstarts.add(target)
                        instruction.op_str = ".L%x" % (target)
                    elif target in container.plt:
                        #XXX: figure out why @PLT is wrong, gcc does not like it
                        # instruction.op_str = "{}@PLT".format(
                            # container.plt[target])
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
                        if target in addr_to_idx:
                            idx = addr_to_idx[target]
                            function.nexts[inst_idx].append(idx)
                        else:
                            instruction.cf_leaves_fn = True
                            function.nexts[inst_idx].append("undef")
                elif is_jmp:
                    function.nexts[inst_idx].append("undef")

    def symbolize_switch_tables(self, container, context):
        return 0
        rodata = container.sections.get(".rodata", None)
        if not rodata:
            return

        all_bases = set([x for _, y in self.pot_sw_bases.items() for x in y])

        for faddr, swbases in self.pot_sw_bases.items():
            fn = container.functions[faddr]

            #XXX: jump tables do not work like this anymore :(
            # for swbase in sorted(swbases, reverse=True):
                # value = rodata.read_at(swbase, 4)
                # if not value:
                    # continue

                # value = (value + swbase) & 0xffffffff
                # debug(hex(swbase) +  hex(value))
                # # if not fn.is_valid_instruction(value):
                    # # continue

                # debug(hex(swbase))
                # # We have a valid switch base now.
                # swlbl = ".LC%x-.LC%x" % (value, swbase)
                # rodata.replace(swbase, 4, swlbl)

                # # Symbolize as long as we can
                # for slot in range(swbase + 4, rodata.base + rodata.sz, 4):
                    # if any([x in all_bases for x in range(slot, slot + 4)]):
                        # break

                    # value = rodata.read_at(slot, 4)
                    # if not value:
                        # break

                    # value = (value + swbase) & 0xFFFFFFFF
                    # if not fn.is_valid_instruction(value):
                        # break

                    # swlbl = ".LC%x-.LC%x" % (value, swbase)
                    # rodata.replace(slot, 4, swlbl)

            for swbase in sorted(swbases, reverse=True):
                if swbase != 0x1c00: continue
                for addr in self.xrefs[swbase]:
                    debug(f"OMG {hex(addr)}")

                    value = rodata.read_at(swbase, 1)
                    # if not value:
                        # continue

                    value = (value*4 + addr) & 0xffffffff
                    # if not fn.is_valid_instruction(value):
                        # continue

                    # We have a valid switch base now.
                    swlbl = ".LC%x-.LC%x" % (value, addr)
                    rodata.replace(swbase, 4, swlbl)

                    # Symbolize as long as we can
                    for slot in range(swbase + 4, rodata.base + rodata.sz, 4):
                        if any([x in all_bases for x in range(slot, slot + 4)]):
                            break

                        value = rodata.read_at(slot, 4)
                        if not value:
                            break

                        value = (value + swbase) & 0xFFFFFFFF
                        if not fn.is_valid_instruction(value):
                            break

                        swlbl = ".LC%x-.LC%x" % (value, swbase)
                        rodata.replace(slot, 4, swlbl)
        # exit(1)

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

    def _adjust_global_access(self, container, function, edx, inst):
        # global variable addresses are dynamically built in multiple instructions
        # here we try to resolve the address with some capstone trickery and shady assumptions
        reg_name = inst.reg_writes()[0]
        original = inst.cs.operands[1].imm
        dereference_resolved = False
        to_fix = []
        for inst2 in function.cache[edx:]:
            if inst2.address <= inst.address: continue
            if reg_name in inst2.reg_reads():
                debug(f"read in {inst2}!!!")
                to_fix += [inst2]
            if reg_name in inst2.reg_writes():
                debug(f"wrote in {inst2}!!!")
                break

        inst.mnemonic = "adrp"
        inst.op_str = "%s, .LC%x" % (reg_name, original)

        for inst2 in to_fix:
            resolved_address = original = inst.cs.operands[1].imm
            if inst2.mnemonic == "add":
                # assert inst2.cs.operands[2].type == CS_OP_IMM
                if not inst2.cs.operands[2].type == CS_OP_IMM:
                    continue
                assert inst2.cs.operands[2].type == CS_OP_IMM
                assert all([op.shift.value == 0 for op in inst2.cs.operands])
                resolved_address += inst2.cs.operands[2].imm
                inst2.mnemonic = "ldr"
                dereference_resolved = False
            elif inst2.mnemonic == "ldr":
                assert inst2.cs.operands[1].type == CS_OP_MEM
                if not all([op.shift.value == 0 for op in inst2.cs.operands]):
                    continue
                resolved_address += inst2.cs.operands[1].mem.disp
                dereference_resolved = True
            elif inst2.mnemonic == "str":
                # assert inst2.cs.operands[1].type == CS_OP_MEM
                # if not all([op.shift.value == 0 for op in inst2.cs.operands]):
                # continue
                resolved_address += inst2.cs.operands[1].mem.disp
                dereference_resolved = True
            else:
                continue


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
            debug(reg_name2)

            if inst2.mnemonic == "str":
                inst2.mnemonic =  "ldr"
                inst2.op_str =  reg_name3 + f", =.LC%x" % (resolved_address)
                inst2.instrument_after(InstrumentedInstruction(
                    f"str {reg_name2}, [{reg_name3}]"))
            elif is_an_import:
                inst2.op_str =  reg_name2 + f", =%s" % (is_an_import)
            else:
                inst2.op_str =  reg_name2 + f", =.LC%x" % (resolved_address)
                if dereference_resolved:
                    if reg_name2.startswith("w"): reg_name2 = 'x' + reg_name2[1:] #XXX: fix this ugly hack
                    inst2.instrument_after(InstrumentedInstruction(
                        f"ldr {reg_name2}, [{reg_name2}]"))

            self.xrefs[resolved_address] += [inst2.address]
            if container.is_in_section(".rodata", resolved_address):
                self.pot_sw_bases[function.start].add(resolved_address)






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

                    if container.is_in_section(".rodata", target):
                        self.pot_sw_bases[function.start].add(target)
                    debug(f"TO:   {inst}")

    def _handle_relocation(self, container, section, rel):
        reloc_type = rel['type']
        # XXX: ARM
        if reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_PC32"]:
            swbase = None
            for base in sorted(self.bases):
                if base > rel['offset']:
                    break
                swbase = base
            value = rel['st_value'] + rel['addend'] - (rel['offset'] - swbase)
            swlbl = ".LC%x-.LC%x" % (value, swbase)
            section.replace(rel['offset'], 4, swlbl)
        elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_64"]:
            value = rel['st_value'] + rel['addend']
            label = ".LC%x" % value
            section.replace(rel['offset'], 8, label)
        # elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_RELATIVE"]:
        elif reloc_type == ENUM_RELOC_TYPE_AARCH64["R_AARCH64_RELATIVE"]:
            value = rel['addend']
            label = ".LC%x" % value
            if int(value) in container.ignore_function_addrs:
                return
            section.replace(rel['offset'], 8, label)
            debug(f"replaced {hex(rel['offset'])} with {label}")
        elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_COPY"]:
            # NOP
            pass
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
