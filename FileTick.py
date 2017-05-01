#!/usr/bin/env python
"""
Reports first tick number in file vs filename
I don't think filename are monotonic w.r.t. ticks.

./FileTick.py ~/H/neo2012-12-25/spool_5/ -xy 320 270 -s 648 -z 4

./FileTick.py ~/data/testdmc

python FileTick.py z:\2017-04-27\spool
"""

from dmcutils.neospool import spoolparam,tickfile,spoolpath

INIFN = 'acquisitionmetadata.ini' # autogen from Solis


if __name__ == '__main__':
    from argparse import ArgumentParser
    p = ArgumentParser()
    p.add_argument('path',help='path to Solis spool files')
    p.add_argument('-o','--tickfn',help='HDF5 file to write with tick vs filename (for reading file in time order)')
    p.add_argument('-xy',help='number of columns,rows',nargs=2,type=int,default=(640,540))
    p.add_argument('-s','--stride',help='number of header bytes',type=int,default=1296)
    p.add_argument('-z','--zerocols',help='number of zero columns',type=int,default=0)
    p = p.parse_args()

    flist = spoolpath(p.path)

    P = spoolparam(flist[0].parent/INIFN, p.xy[0], p.xy[1], p.stride)
    F = tickfile(flist, P, p.tickfn, p.zerocols)
