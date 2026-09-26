def AND_A2(cpu): # A2 AND D
    a = cpu.A
    b = cpu.D
    t = a & b
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def AND_A3(cpu): # A3 AND E
    a = cpu.A
    b = cpu.E
    t = a & b
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def AND_A4(cpu): # A4 AND H
    a = cpu.A
    b = (cpu.HL >> 8)
    t = a & b
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def AND_A5(cpu): # A5 AND L
    a = cpu.A
    b = (cpu.HL & 0xFF)
    t = a & b
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def AND_A6(cpu): # A6 AND (HL)
    a = cpu.A
    b = cpu.mb.getitem(cpu.HL)
    t = a & b
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def AND_A7(cpu): # A7 AND A
    a = cpu.A
    b = cpu.A
    t = a & b
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def XOR_A8(cpu): # A8 XOR B
    a = cpu.A
    b = cpu.B
    t = a ^ b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def XOR_A9(cpu): # A9 XOR C
    a = cpu.A
    b = cpu.C
    t = a ^ b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def XOR_AA(cpu): # AA XOR D
    a = cpu.A
    b = cpu.D
    t = a ^ b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def XOR_AB(cpu): # AB XOR E
    a = cpu.A
    b = cpu.E
    t = a ^ b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def XOR_AC(cpu): # AC XOR H
    a = cpu.A
    b = (cpu.HL >> 8)
    t = a ^ b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def XOR_AD(cpu): # AD XOR L
    a = cpu.A
    b = (cpu.HL & 0xFF)
    t = a ^ b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def XOR_AE(cpu): # AE XOR (HL)
    a = cpu.A
    b = cpu.mb.getitem(cpu.HL)
    t = a ^ b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def XOR_AF(cpu): # AF XOR A
    a = cpu.A
    b = cpu.A
    t = a ^ b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def OR_B0(cpu): # B0 OR B
    a = cpu.A
    b = cpu.B
    t = a | b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def OR_B1(cpu): # B1 OR C
    a = cpu.A
    b = cpu.C
    t = a | b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def OR_B2(cpu): # B2 OR D
    a = cpu.A
    b = cpu.D
    t = a | b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def OR_B3(cpu): # B3 OR E
    a = cpu.A
    b = cpu.E
    t = a | b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def OR_B4(cpu): # B4 OR H
    a = cpu.A
    b = (cpu.HL >> 8)
    t = a | b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def OR_B5(cpu): # B5 OR L
    a = cpu.A
    b = (cpu.HL & 0xFF)
    t = a | b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def OR_B6(cpu): # B6 OR (HL)
    a = cpu.A
    b = cpu.mb.getitem(cpu.HL)
    t = a | b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def OR_B7(cpu): # B7 OR A
    a = cpu.A
    b = cpu.A
    t = a | b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def CP_B8(cpu): # B8 CP B
    a = cpu.A
    b = cpu.B
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    flag |= (t < 0) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def CP_B9(cpu): # B9 CP C
    a = cpu.A
    b = cpu.C
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    flag |= (t < 0) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def CP_BA(cpu): # BA CP D
    a = cpu.A
    b = cpu.D
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    flag |= (t < 0) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def CP_BB(cpu): # BB CP E
    a = cpu.A
    b = cpu.E
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    flag |= (t < 0) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def CP_BC(cpu): # BC CP H
    a = cpu.A
    b = (cpu.HL >> 8)
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    flag |= (t < 0) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def CP_BD(cpu): # BD CP L
    a = cpu.A
    b = (cpu.HL & 0xFF)
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    flag |= (t < 0) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def CP_BE(cpu): # BE CP (HL)
    a = cpu.A
    b = cpu.mb.getitem(cpu.HL)
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    flag |= (t < 0) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def CP_BF(cpu): # BF CP A
    a = cpu.A
    b = cpu.A
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    flag |= (t < 0) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def RET_C0(cpu): # C0 RET NZ
    if ((cpu.F & (1 << FLAGZ)) == 0):
        cpu.PC = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8 # High
        cpu.PC |= cpu.mb.getitem(cpu.SP) # Low
        cpu.SP += 2
        cpu.SP &= 0xFFFF
        cpu.cycles += 20
    else:
        cpu.PC += 1
        cpu.PC &= 0xFFFF
        cpu.cycles += 8


def POP_C1(cpu): # C1 POP BC
    cpu.B = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) # High
    cpu.C = cpu.mb.getitem(cpu.SP) # Low
    cpu.SP += 2
    cpu.SP &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 12


def JP_C2(cpu, v): # C2 JP NZ,a16
    if ((cpu.F & (1 << FLAGZ)) == 0):
        cpu.PC = v
        cpu.cycles += 16
    else:
        cpu.PC += 3
        cpu.PC &= 0xFFFF
        cpu.cycles += 12


def JP_C3(cpu, v): # C3 JP a16
    cpu.PC = v
    cpu.cycles += 16


def CALL_C4(cpu, v): # C4 CALL NZ,a16
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    if ((cpu.F & (1 << FLAGZ)) == 0):
        cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
        cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
        cpu.SP -= 2
        cpu.SP &= 0xFFFF
        cpu.PC = v
        cpu.cycles += 24
    else:
        cpu.cycles += 12


def PUSH_C5(cpu): # C5 PUSH BC
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.B) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.C) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 16


def ADD_C6(cpu, v): # C6 ADD A,d8
    a = cpu.A
    b = v
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RST_C7(cpu): # C7 RST 00H
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC = 0
    cpu.cycles += 16


def RET_C8(cpu): # C8 RET Z
    if ((cpu.F & (1 << FLAGZ)) != 0):
        cpu.PC = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8 # High
        cpu.PC |= cpu.mb.getitem(cpu.SP) # Low
        cpu.SP += 2
        cpu.SP &= 0xFFFF
        cpu.cycles += 20
    else:
        cpu.PC += 1
        cpu.PC &= 0xFFFF
        cpu.cycles += 8


def RET_C9(cpu): # C9 RET
    cpu.PC = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8 # High
    cpu.PC |= cpu.mb.getitem(cpu.SP) # Low
    cpu.SP += 2
    cpu.SP &= 0xFFFF
    cpu.cycles += 16


def JP_CA(cpu, v): # CA JP Z,a16
    if ((cpu.F & (1 << FLAGZ)) != 0):
        cpu.PC = v
        cpu.cycles += 16
    else:
        cpu.PC += 3
        cpu.PC &= 0xFFFF
        cpu.cycles += 12


def PREFIX_CB(cpu): # CB PREFIX CB
    logger.critical('CB cannot be called!')
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def CALL_CC(cpu, v): # CC CALL Z,a16
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    if ((cpu.F & (1 << FLAGZ)) != 0):
        cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
        cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
        cpu.SP -= 2
        cpu.SP &= 0xFFFF
        cpu.PC = v
        cpu.cycles += 24
    else:
        cpu.cycles += 12


def CALL_CD(cpu, v): # CD CALL a16
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC = v
    cpu.cycles += 24


def ADC_CE(cpu, v): # CE ADC A,d8
    a = cpu.A
    b = v
    c = ((cpu.F & (1 << FLAGC)) != 0)
    t = a + b + c
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF) + ((cpu.F & (1 << FLAGC)) != 0)) > 0xF) << FLAGH
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RST_CF(cpu): # CF RST 08H
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC = 8
    cpu.cycles += 16


def RET_D0(cpu): # D0 RET NC
    if ((cpu.F & (1 << FLAGC)) == 0):
        cpu.PC = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8 # High
        cpu.PC |= cpu.mb.getitem(cpu.SP) # Low
        cpu.SP += 2
        cpu.SP &= 0xFFFF
        cpu.cycles += 20
    else:
        cpu.PC += 1
        cpu.PC &= 0xFFFF
        cpu.cycles += 8


def POP_D1(cpu): # D1 POP DE
    cpu.D = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) # High
    cpu.E = cpu.mb.getitem(cpu.SP) # Low
    cpu.SP += 2
    cpu.SP &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 12


def JP_D2(cpu, v): # D2 JP NC,a16
    if ((cpu.F & (1 << FLAGC)) == 0):
        cpu.PC = v
        cpu.cycles += 16
    else:
        cpu.PC += 3
        cpu.PC &= 0xFFFF
        cpu.cycles += 12


def CALL_D4(cpu, v): # D4 CALL NC,a16
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    if ((cpu.F & (1 << FLAGC)) == 0):
        cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
        cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
        cpu.SP -= 2
        cpu.SP &= 0xFFFF
        cpu.PC = v
        cpu.cycles += 24
    else:
        cpu.cycles += 12


def PUSH_D5(cpu): # D5 PUSH DE
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.D) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.E) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 16


def SUB_D6(cpu, v): # D6 SUB d8
    a = cpu.A
    b = v
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    flag |= (t < 0) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RST_D7(cpu): # D7 RST 10H
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC = 16
    cpu.cycles += 16


def RET_D8(cpu): # D8 RET C
    if ((cpu.F & (1 << FLAGC)) != 0):
        cpu.PC = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8 # High
        cpu.PC |= cpu.mb.getitem(cpu.SP) # Low
        cpu.SP += 2
        cpu.SP &= 0xFFFF
        cpu.cycles += 20
    else:
        cpu.PC += 1
        cpu.PC &= 0xFFFF
        cpu.cycles += 8


def RETI_D9(cpu): # D9 RETI
    cpu.interrupt_master_enable = True
    cpu.bail = (cpu.interrupts_flag_register & 0b11111) & (cpu.interrupts_enabled_register & 0b11111)
    cpu.PC = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8 # High
    cpu.PC |= cpu.mb.getitem(cpu.SP) # Low
    cpu.SP += 2
    cpu.SP &= 0xFFFF
    cpu.cycles += 16


def JP_DA(cpu, v): # DA JP C,a16
    if ((cpu.F & (1 << FLAGC)) != 0):
        cpu.PC = v
        cpu.cycles += 16
    else:
        cpu.PC += 3
        cpu.PC &= 0xFFFF
        cpu.cycles += 12


def CALL_DC(cpu, v): # DC CALL C,a16
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    if ((cpu.F & (1 << FLAGC)) != 0):
        cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
        cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
        cpu.SP -= 2
        cpu.SP &= 0xFFFF
        cpu.PC = v
        cpu.cycles += 24
    else:
        cpu.cycles += 12


def SBC_DE(cpu, v): # DE SBC A,d8
    a = cpu.A
    b = v
    c = ((cpu.F & (1 << FLAGC)) != 0)
    t = a - b - c
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF) - ((cpu.F & (1 << FLAGC)) != 0)) < 0) << FLAGH
    flag |= (t < 0) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RST_DF(cpu): # DF RST 18H
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC = 24
    cpu.cycles += 16


def LDH_E0(cpu, v): # E0 LDH (a8),A
    cpu.cycles += 4
    cpu.mb.setitem_io_ports(v | 0xFF00, cpu.A)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def POP_E1(cpu): # E1 POP HL
    cpu.HL = (cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8) + cpu.mb.getitem(cpu.SP) # High
    cpu.SP += 2
    cpu.SP &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 12


def LD_E2(cpu): # E2 LD (C),A
    cpu.mb.setitem_io_ports(0xFF00 | cpu.C, cpu.A)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def PUSH_E5(cpu): # E5 PUSH HL
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.HL >> 8) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.HL & 0xFF) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 16


def AND_E6(cpu, v): # E6 AND d8
    a = cpu.A
    b = v
    t = a & b
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RST_E7(cpu): # E7 RST 20H
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC = 32
    cpu.cycles += 16


def ADD_E8(cpu, v): # E8 ADD SP,r8
    a = cpu.SP
    b = ((v ^ 0x80) - 0x80)
    t = a + b
    flag = 0b00000000
    flag |= (((cpu.SP & 0xF) + (v & 0xF)) > 0xF) << FLAGH
    flag |= (((cpu.SP & 0xFF) + (v & 0xFF)) > 0xFF) << FLAGC
    cpu.F = 0b00000000
    cpu.F |= flag
    t &= 0xFFFF
    cpu.SP = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 16


def JP_E9(cpu): # E9 JP (HL)
    cpu.PC = cpu.HL
    cpu.cycles += 4


def LD_EA(cpu, v): # EA LD (a16),A
    cpu.cycles += 8
    cpu.mb.setitem(v, cpu.A)
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


