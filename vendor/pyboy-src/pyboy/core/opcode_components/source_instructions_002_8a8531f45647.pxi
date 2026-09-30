def LD_55(cpu): # 55 LD D,L
    cpu.D = (cpu.HL & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_56(cpu): # 56 LD D,(HL)
    cpu.D = cpu.mb.getitem(cpu.HL)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_57(cpu): # 57 LD D,A
    cpu.D = cpu.A
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_58(cpu): # 58 LD E,B
    cpu.E = cpu.B
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_59(cpu): # 59 LD E,C
    cpu.E = cpu.C
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_5A(cpu): # 5A LD E,D
    cpu.E = cpu.D
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_5B(cpu): # 5B LD E,E
    cpu.E = cpu.E
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_5C(cpu): # 5C LD E,H
    cpu.E = (cpu.HL >> 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_5D(cpu): # 5D LD E,L
    cpu.E = (cpu.HL & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_5E(cpu): # 5E LD E,(HL)
    cpu.E = cpu.mb.getitem(cpu.HL)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_5F(cpu): # 5F LD E,A
    cpu.E = cpu.A
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_60(cpu): # 60 LD H,B
    cpu.HL = (cpu.HL & 0x00FF) | (cpu.B << 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_61(cpu): # 61 LD H,C
    cpu.HL = (cpu.HL & 0x00FF) | (cpu.C << 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_62(cpu): # 62 LD H,D
    cpu.HL = (cpu.HL & 0x00FF) | (cpu.D << 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_63(cpu): # 63 LD H,E
    cpu.HL = (cpu.HL & 0x00FF) | (cpu.E << 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_64(cpu): # 64 LD H,H
    cpu.HL = (cpu.HL & 0x00FF) | ((cpu.HL >> 8) << 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_65(cpu): # 65 LD H,L
    cpu.HL = (cpu.HL & 0x00FF) | ((cpu.HL & 0xFF) << 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_66(cpu): # 66 LD H,(HL)
    cpu.HL = (cpu.HL & 0x00FF) | (cpu.mb.getitem(cpu.HL) << 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_67(cpu): # 67 LD H,A
    cpu.HL = (cpu.HL & 0x00FF) | (cpu.A << 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_68(cpu): # 68 LD L,B
    cpu.HL = (cpu.HL & 0xFF00) | (cpu.B & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_69(cpu): # 69 LD L,C
    cpu.HL = (cpu.HL & 0xFF00) | (cpu.C & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_6A(cpu): # 6A LD L,D
    cpu.HL = (cpu.HL & 0xFF00) | (cpu.D & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_6B(cpu): # 6B LD L,E
    cpu.HL = (cpu.HL & 0xFF00) | (cpu.E & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_6C(cpu): # 6C LD L,H
    cpu.HL = (cpu.HL & 0xFF00) | ((cpu.HL >> 8) & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_6D(cpu): # 6D LD L,L
    cpu.HL = (cpu.HL & 0xFF00) | ((cpu.HL & 0xFF) & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_6E(cpu): # 6E LD L,(HL)
    cpu.HL = (cpu.HL & 0xFF00) | (cpu.mb.getitem(cpu.HL) & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_6F(cpu): # 6F LD L,A
    cpu.HL = (cpu.HL & 0xFF00) | (cpu.A & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_70(cpu): # 70 LD (HL),B
    cpu.mb.setitem(cpu.HL, cpu.B)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_71(cpu): # 71 LD (HL),C
    cpu.mb.setitem(cpu.HL, cpu.C)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_72(cpu): # 72 LD (HL),D
    cpu.mb.setitem(cpu.HL, cpu.D)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_73(cpu): # 73 LD (HL),E
    cpu.mb.setitem(cpu.HL, cpu.E)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_74(cpu): # 74 LD (HL),H
    cpu.mb.setitem(cpu.HL, (cpu.HL >> 8))
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_75(cpu): # 75 LD (HL),L
    cpu.mb.setitem(cpu.HL, (cpu.HL & 0xFF))
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def HALT_76(cpu): # 76 HALT
    cpu.halted = True
    cpu.bail = True
    cpu.cycles += 4


def LD_77(cpu): # 77 LD (HL),A
    cpu.mb.setitem(cpu.HL, cpu.A)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_78(cpu): # 78 LD A,B
    cpu.A = cpu.B
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_79(cpu): # 79 LD A,C
    cpu.A = cpu.C
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_7A(cpu): # 7A LD A,D
    cpu.A = cpu.D
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_7B(cpu): # 7B LD A,E
    cpu.A = cpu.E
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_7C(cpu): # 7C LD A,H
    cpu.A = (cpu.HL >> 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_7D(cpu): # 7D LD A,L
    cpu.A = (cpu.HL & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_7E(cpu): # 7E LD A,(HL)
    cpu.A = cpu.mb.getitem(cpu.HL)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_7F(cpu): # 7F LD A,A
    cpu.A = cpu.A
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADD_80(cpu): # 80 ADD A,B
    a = cpu.A
    b = cpu.B
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADD_81(cpu): # 81 ADD A,C
    a = cpu.A
    b = cpu.C
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADD_82(cpu): # 82 ADD A,D
    a = cpu.A
    b = cpu.D
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADD_83(cpu): # 83 ADD A,E
    a = cpu.A
    b = cpu.E
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADD_84(cpu): # 84 ADD A,H
    a = cpu.A
    b = (cpu.HL >> 8)
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADD_85(cpu): # 85 ADD A,L
    a = cpu.A
    b = (cpu.HL & 0xFF)
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADD_86(cpu): # 86 ADD A,(HL)
    a = cpu.A
    b = cpu.mb.getitem(cpu.HL)
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def ADD_87(cpu): # 87 ADD A,A
    a = cpu.A
    b = cpu.A
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADC_88(cpu): # 88 ADC A,B
    a = cpu.A
    b = cpu.B
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADC_89(cpu): # 89 ADC A,C
    a = cpu.A
    b = cpu.C
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADC_8A(cpu): # 8A ADC A,D
    a = cpu.A
    b = cpu.D
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADC_8B(cpu): # 8B ADC A,E
    a = cpu.A
    b = cpu.E
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADC_8C(cpu): # 8C ADC A,H
    a = cpu.A
    b = (cpu.HL >> 8)
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADC_8D(cpu): # 8D ADC A,L
    a = cpu.A
    b = (cpu.HL & 0xFF)
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def ADC_8E(cpu): # 8E ADC A,(HL)
    a = cpu.A
    b = cpu.mb.getitem(cpu.HL)
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def ADC_8F(cpu): # 8F ADC A,A
    a = cpu.A
    b = cpu.A
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SUB_90(cpu): # 90 SUB B
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
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SUB_91(cpu): # 91 SUB C
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
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SUB_92(cpu): # 92 SUB D
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
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SUB_93(cpu): # 93 SUB E
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
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SUB_94(cpu): # 94 SUB H
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
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SUB_95(cpu): # 95 SUB L
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
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SUB_96(cpu): # 96 SUB (HL)
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
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SUB_97(cpu): # 97 SUB A
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
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SBC_98(cpu): # 98 SBC A,B
    a = cpu.A
    b = cpu.B
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SBC_99(cpu): # 99 SBC A,C
    a = cpu.A
    b = cpu.C
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SBC_9A(cpu): # 9A SBC A,D
    a = cpu.A
    b = cpu.D
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SBC_9B(cpu): # 9B SBC A,E
    a = cpu.A
    b = cpu.E
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SBC_9C(cpu): # 9C SBC A,H
    a = cpu.A
    b = (cpu.HL >> 8)
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SBC_9D(cpu): # 9D SBC A,L
    a = cpu.A
    b = (cpu.HL & 0xFF)
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def SBC_9E(cpu): # 9E SBC A,(HL)
    a = cpu.A
    b = cpu.mb.getitem(cpu.HL)
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SBC_9F(cpu): # 9F SBC A,A
    a = cpu.A
    b = cpu.A
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
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def AND_A0(cpu): # A0 AND B
    a = cpu.A
    b = cpu.B
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


def AND_A1(cpu): # A1 AND C
    a = cpu.A
    b = cpu.C
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


