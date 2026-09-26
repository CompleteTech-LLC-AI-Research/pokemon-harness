def SET_1E9(cpu): # 1E9 SET 5,C
    t = cpu.C | (1 << 5)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1EA(cpu): # 1EA SET 5,D
    t = cpu.D | (1 << 5)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1EB(cpu): # 1EB SET 5,E
    t = cpu.E | (1 << 5)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1EC(cpu): # 1EC SET 5,H
    t = (cpu.HL >> 8) | (1 << 5)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1ED(cpu): # 1ED SET 5,L
    t = (cpu.HL & 0xFF) | (1 << 5)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1EE(cpu): # 1EE SET 5,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) | (1 << 5)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1EF(cpu): # 1EF SET 5,A
    t = cpu.A | (1 << 5)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1F0(cpu): # 1F0 SET 6,B
    t = cpu.B | (1 << 6)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1F1(cpu): # 1F1 SET 6,C
    t = cpu.C | (1 << 6)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1F2(cpu): # 1F2 SET 6,D
    t = cpu.D | (1 << 6)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1F3(cpu): # 1F3 SET 6,E
    t = cpu.E | (1 << 6)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1F4(cpu): # 1F4 SET 6,H
    t = (cpu.HL >> 8) | (1 << 6)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1F5(cpu): # 1F5 SET 6,L
    t = (cpu.HL & 0xFF) | (1 << 6)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1F6(cpu): # 1F6 SET 6,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) | (1 << 6)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1F7(cpu): # 1F7 SET 6,A
    t = cpu.A | (1 << 6)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1F8(cpu): # 1F8 SET 7,B
    t = cpu.B | (1 << 7)
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1F9(cpu): # 1F9 SET 7,C
    t = cpu.C | (1 << 7)
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1FA(cpu): # 1FA SET 7,D
    t = cpu.D | (1 << 7)
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1FB(cpu): # 1FB SET 7,E
    t = cpu.E | (1 << 7)
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1FC(cpu): # 1FC SET 7,H
    t = (cpu.HL >> 8) | (1 << 7)
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1FD(cpu): # 1FD SET 7,L
    t = (cpu.HL & 0xFF) | (1 << 7)
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1FE(cpu): # 1FE SET 7,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) | (1 << 7)
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SET_1FF(cpu): # 1FF SET 7,A
    t = cpu.A | (1 << 7)
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


