
# THIS FILE IS AUTO-GENERATED!!!
# DO NOT MODIFY THIS FILE.
# CHANGES TO THE CODE SHOULD BE MADE IN 'opcodes_gen.py'.

import array

import pyboy
logger = pyboy.logging.get_logger(__name__)

FLAGC, FLAGH, FLAGN, FLAGZ = range(4, 8)

def BRK(cpu):
    cpu.bail = True
    cpu.mb.breakpoint_singlestep = 1
    cpu.mb.breakpoint_singlestep_latch = 0
    # NOTE: We do not increment PC
    return 0

