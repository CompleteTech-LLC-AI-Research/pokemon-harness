def BIT_17D(cpu): # 17D BIT 7,L
    t = (cpu.HL & 0xFF) & (1 << 7)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_17E(cpu): # 17E BIT 7,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & (1 << 7)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_17F(cpu): # 17F BIT 7,A
    t = cpu.A & (1 << 7)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_180(cpu): # 180 RES 0,B
    t = cpu.B & ~(1 << 0)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_181(cpu): # 181 RES 0,C
    t = cpu.C & ~(1 << 0)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_182(cpu): # 182 RES 0,D
    t = cpu.D & ~(1 << 0)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_183(cpu): # 183 RES 0,E
    t = cpu.E & ~(1 << 0)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_184(cpu): # 184 RES 0,H
    t = (cpu.HL >> 8) & ~(1 << 0)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_185(cpu): # 185 RES 0,L
    t = (cpu.HL & 0xFF) & ~(1 << 0)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_186(cpu): # 186 RES 0,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & ~(1 << 0)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_187(cpu): # 187 RES 0,A
    t = cpu.A & ~(1 << 0)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_188(cpu): # 188 RES 1,B
    t = cpu.B & ~(1 << 1)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_189(cpu): # 189 RES 1,C
    t = cpu.C & ~(1 << 1)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_18A(cpu): # 18A RES 1,D
    t = cpu.D & ~(1 << 1)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_18B(cpu): # 18B RES 1,E
    t = cpu.E & ~(1 << 1)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_18C(cpu): # 18C RES 1,H
    t = (cpu.HL >> 8) & ~(1 << 1)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_18D(cpu): # 18D RES 1,L
    t = (cpu.HL & 0xFF) & ~(1 << 1)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_18E(cpu): # 18E RES 1,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & ~(1 << 1)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_18F(cpu): # 18F RES 1,A
    t = cpu.A & ~(1 << 1)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_190(cpu): # 190 RES 2,B
    t = cpu.B & ~(1 << 2)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_191(cpu): # 191 RES 2,C
    t = cpu.C & ~(1 << 2)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_192(cpu): # 192 RES 2,D
    t = cpu.D & ~(1 << 2)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_193(cpu): # 193 RES 2,E
    t = cpu.E & ~(1 << 2)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_194(cpu): # 194 RES 2,H
    t = (cpu.HL >> 8) & ~(1 << 2)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_195(cpu): # 195 RES 2,L
    t = (cpu.HL & 0xFF) & ~(1 << 2)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_196(cpu): # 196 RES 2,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & ~(1 << 2)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_197(cpu): # 197 RES 2,A
    t = cpu.A & ~(1 << 2)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_198(cpu): # 198 RES 3,B
    t = cpu.B & ~(1 << 3)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_199(cpu): # 199 RES 3,C
    t = cpu.C & ~(1 << 3)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_19A(cpu): # 19A RES 3,D
    t = cpu.D & ~(1 << 3)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_19B(cpu): # 19B RES 3,E
    t = cpu.E & ~(1 << 3)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_19C(cpu): # 19C RES 3,H
    t = (cpu.HL >> 8) & ~(1 << 3)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_19D(cpu): # 19D RES 3,L
    t = (cpu.HL & 0xFF) & ~(1 << 3)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_19E(cpu): # 19E RES 3,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & ~(1 << 3)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_19F(cpu): # 19F RES 3,A
    t = cpu.A & ~(1 << 3)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1A0(cpu): # 1A0 RES 4,B
    t = cpu.B & ~(1 << 4)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1A1(cpu): # 1A1 RES 4,C
    t = cpu.C & ~(1 << 4)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1A2(cpu): # 1A2 RES 4,D
    t = cpu.D & ~(1 << 4)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1A3(cpu): # 1A3 RES 4,E
    t = cpu.E & ~(1 << 4)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1A4(cpu): # 1A4 RES 4,H
    t = (cpu.HL >> 8) & ~(1 << 4)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1A5(cpu): # 1A5 RES 4,L
    t = (cpu.HL & 0xFF) & ~(1 << 4)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1A6(cpu): # 1A6 RES 4,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & ~(1 << 4)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1A7(cpu): # 1A7 RES 4,A
    t = cpu.A & ~(1 << 4)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1A8(cpu): # 1A8 RES 5,B
    t = cpu.B & ~(1 << 5)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1A9(cpu): # 1A9 RES 5,C
    t = cpu.C & ~(1 << 5)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1AA(cpu): # 1AA RES 5,D
    t = cpu.D & ~(1 << 5)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1AB(cpu): # 1AB RES 5,E
    t = cpu.E & ~(1 << 5)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1AC(cpu): # 1AC RES 5,H
    t = (cpu.HL >> 8) & ~(1 << 5)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1AD(cpu): # 1AD RES 5,L
    t = (cpu.HL & 0xFF) & ~(1 << 5)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1AE(cpu): # 1AE RES 5,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & ~(1 << 5)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1AF(cpu): # 1AF RES 5,A
    t = cpu.A & ~(1 << 5)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1B0(cpu): # 1B0 RES 6,B
    t = cpu.B & ~(1 << 6)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1B1(cpu): # 1B1 RES 6,C
    t = cpu.C & ~(1 << 6)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1B2(cpu): # 1B2 RES 6,D
    t = cpu.D & ~(1 << 6)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1B3(cpu): # 1B3 RES 6,E
    t = cpu.E & ~(1 << 6)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1B4(cpu): # 1B4 RES 6,H
    t = (cpu.HL >> 8) & ~(1 << 6)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1B5(cpu): # 1B5 RES 6,L
    t = (cpu.HL & 0xFF) & ~(1 << 6)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1B6(cpu): # 1B6 RES 6,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & ~(1 << 6)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1B7(cpu): # 1B7 RES 6,A
    t = cpu.A & ~(1 << 6)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1B8(cpu): # 1B8 RES 7,B
    t = cpu.B & ~(1 << 7)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1B9(cpu): # 1B9 RES 7,C
    t = cpu.C & ~(1 << 7)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1BA(cpu): # 1BA RES 7,D
    t = cpu.D & ~(1 << 7)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1BB(cpu): # 1BB RES 7,E
    t = cpu.E & ~(1 << 7)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1BC(cpu): # 1BC RES 7,H
    t = (cpu.HL >> 8) & ~(1 << 7)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1BD(cpu): # 1BD RES 7,L
    t = (cpu.HL & 0xFF) & ~(1 << 7)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1BE(cpu): # 1BE RES 7,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & ~(1 << 7)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def RES_1BF(cpu): # 1BF RES 7,A
    t = cpu.A & ~(1 << 7)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1C0(cpu): # 1C0 SET 0,B
    t = cpu.B | (1 << 0)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1C1(cpu): # 1C1 SET 0,C
    t = cpu.C | (1 << 0)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1C2(cpu): # 1C2 SET 0,D
    t = cpu.D | (1 << 0)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1C3(cpu): # 1C3 SET 0,E
    t = cpu.E | (1 << 0)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1C4(cpu): # 1C4 SET 0,H
    t = (cpu.HL >> 8) | (1 << 0)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1C5(cpu): # 1C5 SET 0,L
    t = (cpu.HL & 0xFF) | (1 << 0)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1C6(cpu): # 1C6 SET 0,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) | (1 << 0)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1C7(cpu): # 1C7 SET 0,A
    t = cpu.A | (1 << 0)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1C8(cpu): # 1C8 SET 1,B
    t = cpu.B | (1 << 1)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1C9(cpu): # 1C9 SET 1,C
    t = cpu.C | (1 << 1)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1CA(cpu): # 1CA SET 1,D
    t = cpu.D | (1 << 1)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1CB(cpu): # 1CB SET 1,E
    t = cpu.E | (1 << 1)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1CC(cpu): # 1CC SET 1,H
    t = (cpu.HL >> 8) | (1 << 1)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1CD(cpu): # 1CD SET 1,L
    t = (cpu.HL & 0xFF) | (1 << 1)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1CE(cpu): # 1CE SET 1,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) | (1 << 1)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1CF(cpu): # 1CF SET 1,A
    t = cpu.A | (1 << 1)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1D0(cpu): # 1D0 SET 2,B
    t = cpu.B | (1 << 2)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1D1(cpu): # 1D1 SET 2,C
    t = cpu.C | (1 << 2)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1D2(cpu): # 1D2 SET 2,D
    t = cpu.D | (1 << 2)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1D3(cpu): # 1D3 SET 2,E
    t = cpu.E | (1 << 2)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1D4(cpu): # 1D4 SET 2,H
    t = (cpu.HL >> 8) | (1 << 2)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1D5(cpu): # 1D5 SET 2,L
    t = (cpu.HL & 0xFF) | (1 << 2)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1D6(cpu): # 1D6 SET 2,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) | (1 << 2)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1D7(cpu): # 1D7 SET 2,A
    t = cpu.A | (1 << 2)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1D8(cpu): # 1D8 SET 3,B
    t = cpu.B | (1 << 3)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1D9(cpu): # 1D9 SET 3,C
    t = cpu.C | (1 << 3)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1DA(cpu): # 1DA SET 3,D
    t = cpu.D | (1 << 3)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1DB(cpu): # 1DB SET 3,E
    t = cpu.E | (1 << 3)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1DC(cpu): # 1DC SET 3,H
    t = (cpu.HL >> 8) | (1 << 3)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1DD(cpu): # 1DD SET 3,L
    t = (cpu.HL & 0xFF) | (1 << 3)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1DE(cpu): # 1DE SET 3,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) | (1 << 3)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1DF(cpu): # 1DF SET 3,A
    t = cpu.A | (1 << 3)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1E0(cpu): # 1E0 SET 4,B
    t = cpu.B | (1 << 4)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1E1(cpu): # 1E1 SET 4,C
    t = cpu.C | (1 << 4)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1E2(cpu): # 1E2 SET 4,D
    t = cpu.D | (1 << 4)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1E3(cpu): # 1E3 SET 4,E
    t = cpu.E | (1 << 4)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1E4(cpu): # 1E4 SET 4,H
    t = (cpu.HL >> 8) | (1 << 4)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1E5(cpu): # 1E5 SET 4,L
    t = (cpu.HL & 0xFF) | (1 << 4)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1E6(cpu): # 1E6 SET 4,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) | (1 << 4)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1E7(cpu): # 1E7 SET 4,A
    t = cpu.A | (1 << 4)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1E8(cpu): # 1E8 SET 5,B
    t = cpu.B | (1 << 5)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


