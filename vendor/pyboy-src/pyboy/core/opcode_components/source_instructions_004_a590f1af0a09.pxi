def XOR_EE(cpu, v): # EE XOR d8
    a = cpu.A
    b = v
    t = a ^ b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RST_EF(cpu): # EF RST 28H
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC = 40
    cpu.cycles += 16


def LDH_F0(cpu, v): # F0 LDH A,(a8)
    cpu.cycles += 4
    cpu.A = cpu.mb.getitem_io_ports(v | 0xFF00)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def POP_F1(cpu): # F1 POP AF
    cpu.A = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) # High
    cpu.F = cpu.mb.getitem(cpu.SP) & 0xF0 & 0xF0 # Low
    cpu.SP += 2
    cpu.SP &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 12


def LD_F2(cpu): # F2 LD A,(C)
    cpu.A = cpu.mb.getitem_io_ports(0xFF00 | cpu.C)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def DI_F3(cpu): # F3 DI
    cpu.interrupt_master_enable = False
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def PUSH_F5(cpu): # F5 PUSH AF
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.A) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.F & 0xF0) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 16


def OR_F6(cpu, v): # F6 OR d8
    a = cpu.A
    b = v
    t = a | b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RST_F7(cpu): # F7 RST 30H
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC = 48
    cpu.cycles += 16


def LD_F8(cpu, v): # F8 LD HL,SP+r8
    cpu.HL = cpu.SP + ((v ^ 0x80) - 0x80)
    t = cpu.HL
    flag = 0b00000000
    flag |= (((cpu.SP & 0xF) + (v & 0xF)) > 0xF) << FLAGH
    flag |= (((cpu.SP & 0xFF) + (v & 0xFF)) > 0xFF) << FLAGC
    cpu.F = 0b00000000
    cpu.F |= flag
    cpu.HL &= 0xFFFF
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 12


def LD_F9(cpu): # F9 LD SP,HL
    cpu.SP = cpu.HL
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_FA(cpu, v): # FA LD A,(a16)
    cpu.cycles += 8
    cpu.A = cpu.mb.getitem(v)
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def EI_FB(cpu): # FB EI
    cpu.interrupt_master_enable = True
    cpu.bail = (cpu.interrupts_flag_register & 0b11111) & (cpu.interrupts_enabled_register & 0b11111)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def CP_FE(cpu, v): # FE CP d8
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
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RST_FF(cpu): # FF RST 38H
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High
    cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low
    cpu.SP -= 2
    cpu.SP &= 0xFFFF
    cpu.PC = 56
    cpu.cycles += 16


def RLC_100(cpu): # 100 RLC B
    a = cpu.B
    t = (a << 1) | (a >> 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RLC_101(cpu): # 101 RLC C
    a = cpu.C
    t = (a << 1) | (a >> 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RLC_102(cpu): # 102 RLC D
    a = cpu.D
    t = (a << 1) | (a >> 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RLC_103(cpu): # 103 RLC E
    a = cpu.E
    t = (a << 1) | (a >> 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RLC_104(cpu): # 104 RLC H
    a = (cpu.HL >> 8)
    t = (a << 1) | (a >> 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RLC_105(cpu): # 105 RLC L
    a = (cpu.HL & 0xFF)
    t = (a << 1) | (a >> 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RLC_106(cpu): # 106 RLC (HL)
    cpu.cycles += 4
    a = cpu.mb.getitem(cpu.HL)
    t = (a << 1) | (a >> 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RLC_107(cpu): # 107 RLC A
    a = cpu.A
    t = (a << 1) | (a >> 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RRC_108(cpu): # 108 RRC B
    a = cpu.B
    t = (a >> 1) | ((a & 1) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RRC_109(cpu): # 109 RRC C
    a = cpu.C
    t = (a >> 1) | ((a & 1) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RRC_10A(cpu): # 10A RRC D
    a = cpu.D
    t = (a >> 1) | ((a & 1) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RRC_10B(cpu): # 10B RRC E
    a = cpu.E
    t = (a >> 1) | ((a & 1) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RRC_10C(cpu): # 10C RRC H
    a = (cpu.HL >> 8)
    t = (a >> 1) | ((a & 1) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RRC_10D(cpu): # 10D RRC L
    a = (cpu.HL & 0xFF)
    t = (a >> 1) | ((a & 1) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RRC_10E(cpu): # 10E RRC (HL)
    cpu.cycles += 4
    a = cpu.mb.getitem(cpu.HL)
    t = (a >> 1) | ((a & 1) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RRC_10F(cpu): # 10F RRC A
    a = cpu.A
    t = (a >> 1) | ((a & 1) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RL_110(cpu): # 110 RL B
    a = cpu.B
    t = (a << 1) | ((cpu.F & (1 << FLAGC)) != 0)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RL_111(cpu): # 111 RL C
    a = cpu.C
    t = (a << 1) | ((cpu.F & (1 << FLAGC)) != 0)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RL_112(cpu): # 112 RL D
    a = cpu.D
    t = (a << 1) | ((cpu.F & (1 << FLAGC)) != 0)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RL_113(cpu): # 113 RL E
    a = cpu.E
    t = (a << 1) | ((cpu.F & (1 << FLAGC)) != 0)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RL_114(cpu): # 114 RL H
    a = (cpu.HL >> 8)
    t = (a << 1) | ((cpu.F & (1 << FLAGC)) != 0)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RL_115(cpu): # 115 RL L
    a = (cpu.HL & 0xFF)
    t = (a << 1) | ((cpu.F & (1 << FLAGC)) != 0)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RL_116(cpu): # 116 RL (HL)
    cpu.cycles += 4
    a = cpu.mb.getitem(cpu.HL)
    t = (a << 1) | ((cpu.F & (1 << FLAGC)) != 0)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RL_117(cpu): # 117 RL A
    a = cpu.A
    t = (a << 1) | ((cpu.F & (1 << FLAGC)) != 0)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RR_118(cpu): # 118 RR B
    a = cpu.B
    t = (a >> 1) | (((cpu.F & (1 << FLAGC)) != 0) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RR_119(cpu): # 119 RR C
    a = cpu.C
    t = (a >> 1) | (((cpu.F & (1 << FLAGC)) != 0) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RR_11A(cpu): # 11A RR D
    a = cpu.D
    t = (a >> 1) | (((cpu.F & (1 << FLAGC)) != 0) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RR_11B(cpu): # 11B RR E
    a = cpu.E
    t = (a >> 1) | (((cpu.F & (1 << FLAGC)) != 0) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RR_11C(cpu): # 11C RR H
    a = (cpu.HL >> 8)
    t = (a >> 1) | (((cpu.F & (1 << FLAGC)) != 0) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RR_11D(cpu): # 11D RR L
    a = (cpu.HL & 0xFF)
    t = (a >> 1) | (((cpu.F & (1 << FLAGC)) != 0) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RR_11E(cpu): # 11E RR (HL)
    cpu.cycles += 4
    a = cpu.mb.getitem(cpu.HL)
    t = (a >> 1) | (((cpu.F & (1 << FLAGC)) != 0) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RR_11F(cpu): # 11F RR A
    a = cpu.A
    t = (a >> 1) | (((cpu.F & (1 << FLAGC)) != 0) << 7)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SLA_120(cpu): # 120 SLA B
    t = (cpu.B << 1)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SLA_121(cpu): # 121 SLA C
    t = (cpu.C << 1)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SLA_122(cpu): # 122 SLA D
    t = (cpu.D << 1)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SLA_123(cpu): # 123 SLA E
    t = (cpu.E << 1)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SLA_124(cpu): # 124 SLA H
    t = ((cpu.HL >> 8) << 1)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SLA_125(cpu): # 125 SLA L
    t = ((cpu.HL & 0xFF) << 1)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SLA_126(cpu): # 126 SLA (HL)
    cpu.cycles += 4
    t = (cpu.mb.getitem(cpu.HL) << 1)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SLA_127(cpu): # 127 SLA A
    t = (cpu.A << 1)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRA_128(cpu): # 128 SRA B
    a = cpu.B
    t = ((a >> 1) | (a & 0x80)) | ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRA_129(cpu): # 129 SRA C
    a = cpu.C
    t = ((a >> 1) | (a & 0x80)) | ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRA_12A(cpu): # 12A SRA D
    a = cpu.D
    t = ((a >> 1) | (a & 0x80)) | ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRA_12B(cpu): # 12B SRA E
    a = cpu.E
    t = ((a >> 1) | (a & 0x80)) | ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRA_12C(cpu): # 12C SRA H
    a = (cpu.HL >> 8)
    t = ((a >> 1) | (a & 0x80)) | ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRA_12D(cpu): # 12D SRA L
    a = (cpu.HL & 0xFF)
    t = ((a >> 1) | (a & 0x80)) | ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRA_12E(cpu): # 12E SRA (HL)
    cpu.cycles += 4
    a = cpu.mb.getitem(cpu.HL)
    t = ((a >> 1) | (a & 0x80)) | ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRA_12F(cpu): # 12F SRA A
    a = cpu.A
    t = ((a >> 1) | (a & 0x80)) | ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SWAP_130(cpu): # 130 SWAP B
    a = cpu.B
    t = ((a & 0xF0) >> 4) | ((a & 0x0F) << 4)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


