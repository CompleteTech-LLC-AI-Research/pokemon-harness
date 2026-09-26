def NOP_00(cpu): # 00 NOP
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_01(cpu, v): # 01 LD BC,d16
    cpu.B = v >> 8
    cpu.C = v & 0x00FF
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    cpu.cycles += 12


def LD_02(cpu): # 02 LD (BC),A
    cpu.mb.setitem(((cpu.B << 8) + cpu.C), cpu.A)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_03(cpu): # 03 INC BC
    a = ((cpu.B << 8) + cpu.C)
    b = 1
    t = a + b
    # No flag operations
    t &= 0xFFFF
    cpu.B = t >> 8
    cpu.C = t & 0x00FF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_04(cpu): # 04 INC B
    a = cpu.B
    b = 1
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.B = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def DEC_05(cpu): # 05 DEC B
    a = cpu.B
    b = 1
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.B = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_06(cpu, v): # 06 LD B,d8
    cpu.B = v
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RLCA_07(cpu): # 07 RLCA
    a = cpu.A
    t = (a << 1) | (a >> 7)
    flag = 0b00000000
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_08(cpu, v): # 08 LD (a16),SP
    cpu.mb.setitem(v, cpu.SP & 0xFF)
    cpu.mb.setitem(v+1, cpu.SP >> 8)
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    cpu.cycles += 20


def ADD_09(cpu): # 09 ADD HL,BC
    a = cpu.HL
    b = ((cpu.B << 8) + cpu.C)
    t = a + b
    flag = 0b00000000
    flag |= (((a & 0xFFF) + (b & 0xFFF)) > 0xFFF) << FLAGH
    flag |= (t > 0xFFFF) << FLAGC
    cpu.F &= 0b10000000
    cpu.F |= flag
    t &= 0xFFFF
    cpu.HL = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_0A(cpu): # 0A LD A,(BC)
    cpu.A = cpu.mb.getitem(((cpu.B << 8) + cpu.C))
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def DEC_0B(cpu): # 0B DEC BC
    a = ((cpu.B << 8) + cpu.C)
    b = 1
    t = a - b
    # No flag operations
    t &= 0xFFFF
    cpu.B = t >> 8
    cpu.C = t & 0x00FF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_0C(cpu): # 0C INC C
    a = cpu.C
    b = 1
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.C = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def DEC_0D(cpu): # 0D DEC C
    a = cpu.C
    b = 1
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.C = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_0E(cpu, v): # 0E LD C,d8
    cpu.C = v
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RRCA_0F(cpu): # 0F RRCA
    a = cpu.A
    t = (a >> 1) | ((a & 1) << 7)
    flag = 0b00000000
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def STOP_10(cpu, v): # 10 STOP 0
    if cpu.mb.cgb:
        cpu.mb.switch_speed()
        cpu.mb.setitem(0xFF04, 0)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_11(cpu, v): # 11 LD DE,d16
    cpu.D = v >> 8
    cpu.E = v & 0x00FF
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    cpu.cycles += 12


def LD_12(cpu): # 12 LD (DE),A
    cpu.mb.setitem(((cpu.D << 8) + cpu.E), cpu.A)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_13(cpu): # 13 INC DE
    a = ((cpu.D << 8) + cpu.E)
    b = 1
    t = a + b
    # No flag operations
    t &= 0xFFFF
    cpu.D = t >> 8
    cpu.E = t & 0x00FF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_14(cpu): # 14 INC D
    a = cpu.D
    b = 1
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.D = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def DEC_15(cpu): # 15 DEC D
    a = cpu.D
    b = 1
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.D = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_16(cpu, v): # 16 LD D,d8
    cpu.D = v
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RLA_17(cpu): # 17 RLA
    a = cpu.A
    t = (a << 1) | ((cpu.F & (1 << FLAGC)) != 0)
    flag = 0b00000000
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def JR_18(cpu, v): # 18 JR r8
    cpu.PC += 2 + ((v ^ 0x80) - 0x80)
    cpu.PC &= 0xFFFF
    cpu.cycles += 12


def ADD_19(cpu): # 19 ADD HL,DE
    a = cpu.HL
    b = ((cpu.D << 8) + cpu.E)
    t = a + b
    flag = 0b00000000
    flag |= (((a & 0xFFF) + (b & 0xFFF)) > 0xFFF) << FLAGH
    flag |= (t > 0xFFFF) << FLAGC
    cpu.F &= 0b10000000
    cpu.F |= flag
    t &= 0xFFFF
    cpu.HL = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_1A(cpu): # 1A LD A,(DE)
    cpu.A = cpu.mb.getitem(((cpu.D << 8) + cpu.E))
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def DEC_1B(cpu): # 1B DEC DE
    a = ((cpu.D << 8) + cpu.E)
    b = 1
    t = a - b
    # No flag operations
    t &= 0xFFFF
    cpu.D = t >> 8
    cpu.E = t & 0x00FF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_1C(cpu): # 1C INC E
    a = cpu.E
    b = 1
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.E = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def DEC_1D(cpu): # 1D DEC E
    a = cpu.E
    b = 1
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.E = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_1E(cpu, v): # 1E LD E,d8
    cpu.E = v
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RRA_1F(cpu): # 1F RRA
    a = cpu.A
    t = (a >> 1) | (((cpu.F & (1 << FLAGC)) != 0) << 7)
    flag = 0b00000000
    flag |= (a & 1) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def JR_20(cpu, v): # 20 JR NZ,r8
    cpu.PC += 2
    if ((cpu.F & (1 << FLAGZ)) == 0):
        cpu.PC += ((v ^ 0x80) - 0x80)
        cpu.PC &= 0xFFFF
        cpu.cycles += 12
    else:
        cpu.PC &= 0xFFFF
        cpu.cycles += 8


def LD_21(cpu, v): # 21 LD HL,d16
    cpu.HL = v
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    cpu.cycles += 12


def LD_22(cpu): # 22 LD (HL+),A
    cpu.mb.setitem(cpu.HL, cpu.A)
    cpu.HL += 1
    cpu.HL &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_23(cpu): # 23 INC HL
    a = cpu.HL
    b = 1
    t = a + b
    # No flag operations
    t &= 0xFFFF
    cpu.HL = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_24(cpu): # 24 INC H
    a = (cpu.HL >> 8)
    b = 1
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def DEC_25(cpu): # 25 DEC H
    a = (cpu.HL >> 8)
    b = 1
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_26(cpu, v): # 26 LD H,d8
    cpu.HL = (cpu.HL & 0x00FF) | (v << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def DAA_27(cpu): # 27 DAA
    t = cpu.A
    corr = 0
    corr |= 0x06 if ((cpu.F & (1 << FLAGH)) != 0) else 0x00
    corr |= 0x60 if ((cpu.F & (1 << FLAGC)) != 0) else 0x00
    if (cpu.F & (1 << FLAGN)) != 0:
        t -= corr
    else:
        corr |= 0x06 if (t & 0x0F) > 0x09 else 0x00
        corr |= 0x60 if t > 0x99 else 0x00
        t += corr
    flag = 0
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (corr & 0x60 != 0) << FLAGC
    cpu.F &= 0b01000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def JR_28(cpu, v): # 28 JR Z,r8
    cpu.PC += 2
    if ((cpu.F & (1 << FLAGZ)) != 0):
        cpu.PC += ((v ^ 0x80) - 0x80)
        cpu.PC &= 0xFFFF
        cpu.cycles += 12
    else:
        cpu.PC &= 0xFFFF
        cpu.cycles += 8


def ADD_29(cpu): # 29 ADD HL,HL
    a = cpu.HL
    b = cpu.HL
    t = a + b
    flag = 0b00000000
    flag |= (((a & 0xFFF) + (b & 0xFFF)) > 0xFFF) << FLAGH
    flag |= (t > 0xFFFF) << FLAGC
    cpu.F &= 0b10000000
    cpu.F |= flag
    t &= 0xFFFF
    cpu.HL = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_2A(cpu): # 2A LD A,(HL+)
    cpu.A = cpu.mb.getitem(cpu.HL)
    cpu.HL += 1
    cpu.HL &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def DEC_2B(cpu): # 2B DEC HL
    a = cpu.HL
    b = 1
    t = a - b
    # No flag operations
    t &= 0xFFFF
    cpu.HL = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_2C(cpu): # 2C INC L
    a = (cpu.HL & 0xFF)
    b = 1
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def DEC_2D(cpu): # 2D DEC L
    a = (cpu.HL & 0xFF)
    b = 1
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_2E(cpu, v): # 2E LD L,d8
    cpu.HL = (cpu.HL & 0xFF00) | (v & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def CPL_2F(cpu): # 2F CPL
    cpu.A = (~cpu.A) & 0xFF
    flag = 0b01100000
    cpu.F &= 0b10010000
    cpu.F |= flag
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def JR_30(cpu, v): # 30 JR NC,r8
    cpu.PC += 2
    if ((cpu.F & (1 << FLAGC)) == 0):
        cpu.PC += ((v ^ 0x80) - 0x80)
        cpu.PC &= 0xFFFF
        cpu.cycles += 12
    else:
        cpu.PC &= 0xFFFF
        cpu.cycles += 8


def LD_31(cpu, v): # 31 LD SP,d16
    cpu.SP = v
    cpu.PC += 3
    cpu.PC &= 0xFFFF
    cpu.cycles += 12


def LD_32(cpu): # 32 LD (HL-),A
    cpu.mb.setitem(cpu.HL, cpu.A)
    cpu.HL -= 1
    cpu.HL &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_33(cpu): # 33 INC SP
    a = cpu.SP
    b = 1
    t = a + b
    # No flag operations
    t &= 0xFFFF
    cpu.SP = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_34(cpu): # 34 INC (HL)
    a = cpu.mb.getitem(cpu.HL)
    b = 1
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def DEC_35(cpu): # 35 DEC (HL)
    a = cpu.mb.getitem(cpu.HL)
    b = 1
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_36(cpu, v): # 36 LD (HL),d8
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, v)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SCF_37(cpu): # 37 SCF
    flag = 0b00010000
    cpu.F &= 0b10000000
    cpu.F |= flag
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def JR_38(cpu, v): # 38 JR C,r8
    cpu.PC += 2
    if ((cpu.F & (1 << FLAGC)) != 0):
        cpu.PC += ((v ^ 0x80) - 0x80)
        cpu.PC &= 0xFFFF
        cpu.cycles += 12
    else:
        cpu.PC &= 0xFFFF
        cpu.cycles += 8


def ADD_39(cpu): # 39 ADD HL,SP
    a = cpu.HL
    b = cpu.SP
    t = a + b
    flag = 0b00000000
    flag |= (((a & 0xFFF) + (b & 0xFFF)) > 0xFFF) << FLAGH
    flag |= (t > 0xFFFF) << FLAGC
    cpu.F &= 0b10000000
    cpu.F |= flag
    t &= 0xFFFF
    cpu.HL = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_3A(cpu): # 3A LD A,(HL-)
    cpu.A = cpu.mb.getitem(cpu.HL)
    cpu.HL -= 1
    cpu.HL &= 0xFFFF
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def DEC_3B(cpu): # 3B DEC SP
    a = cpu.SP
    b = 1
    t = a - b
    # No flag operations
    t &= 0xFFFF
    cpu.SP = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def INC_3C(cpu): # 3C INC A
    a = cpu.A
    b = 1
    t = a + b
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) + (b & 0xF)) > 0xF) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def DEC_3D(cpu): # 3D DEC A
    a = cpu.A
    b = 1
    t = a - b
    flag = 0b01000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (((a & 0xF) - (b & 0xF)) < 0) << FLAGH
    cpu.F &= 0b00010000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_3E(cpu, v): # 3E LD A,d8
    cpu.A = v
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def CCF_3F(cpu): # 3F CCF
    flag = (cpu.F & 0b00010000) ^ 0b00010000
    cpu.F &= 0b10000000
    cpu.F |= flag
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_40(cpu): # 40 LD B,B
    cpu.B = cpu.B
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_41(cpu): # 41 LD B,C
    cpu.B = cpu.C
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_42(cpu): # 42 LD B,D
    cpu.B = cpu.D
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_43(cpu): # 43 LD B,E
    cpu.B = cpu.E
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_44(cpu): # 44 LD B,H
    cpu.B = (cpu.HL >> 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_45(cpu): # 45 LD B,L
    cpu.B = (cpu.HL & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_46(cpu): # 46 LD B,(HL)
    cpu.B = cpu.mb.getitem(cpu.HL)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_47(cpu): # 47 LD B,A
    cpu.B = cpu.A
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_48(cpu): # 48 LD C,B
    cpu.C = cpu.B
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_49(cpu): # 49 LD C,C
    cpu.C = cpu.C
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_4A(cpu): # 4A LD C,D
    cpu.C = cpu.D
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_4B(cpu): # 4B LD C,E
    cpu.C = cpu.E
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_4C(cpu): # 4C LD C,H
    cpu.C = (cpu.HL >> 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_4D(cpu): # 4D LD C,L
    cpu.C = (cpu.HL & 0xFF)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_4E(cpu): # 4E LD C,(HL)
    cpu.C = cpu.mb.getitem(cpu.HL)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def LD_4F(cpu): # 4F LD C,A
    cpu.C = cpu.A
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_50(cpu): # 50 LD D,B
    cpu.D = cpu.B
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_51(cpu): # 51 LD D,C
    cpu.D = cpu.C
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_52(cpu): # 52 LD D,D
    cpu.D = cpu.D
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_53(cpu): # 53 LD D,E
    cpu.D = cpu.E
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


def LD_54(cpu): # 54 LD D,H
    cpu.D = (cpu.HL >> 8)
    cpu.PC += 1
    cpu.PC &= 0xFFFF
    cpu.cycles += 4


