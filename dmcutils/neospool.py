#!/usr/bin/env python
from pathlib import Path
from time import time,sleep
import logging
from configparser import ConfigParser
from datetime import datetime
from pytz import UTC
import numpy as np
from scipy.ndimage import imread
from scipy.misc import imsave
import h5py
from pandas import Series,read_hdf
try:
    import cv2
except ImportError:
    cv2=None  #fall back to scipy imsave, no time annotation
#
from . import mean16to8
from histutils import setupimgh5
from histutils.timedmc import frame2ut1

DTYPE = np.uint16

def preview_newest(path:Path, odir:Path, oldfset:set=None, inifn:str='acquisitionmetadata.ini', verbose:bool=False):
    root = Path(path).expanduser()

    if (root/'image.bmp').is_file():
        f8bit = imread(root/'image.bmp') # TODO check for 8 bit
    elif root.is_dir(): # spool case
#%% find newest file to extract images from
        newfn,oldfset = findnewest(root, oldfset, verbose)
        if newfn is None:
            return oldfset
#%% read images and FPGA tick clock from this file
        P = spoolparam(newfn.parent / inifn)
        sleep(0.5) # to avoid reading newest file while it's still being written
        frames,ticks,tsec = readNeoSpool(newfn, P)
#%% 16 bit to 8 bit, mean of image stack for this file
        f8bit = mean16to8(frames)
    else:
        raise ValueError(f'unknown image file/location {root}')

#%% put time on image and write to disk
    annowrite(f8bit, newfn, odir)

    return oldfset


def findnewest(path:Path, oldset:set=None, verbose:bool=False):
    assert path, f'{path} is empty'
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f'{path}: could not find')
#%% it's a file
    if path.is_file():
        return path
#%% it's a directory
    newset = set(path.glob('*.dat'))
    if not newset:
        raise FileNotFoundError(f'no files found in {path}')

    fset = newset.symmetric_difference(oldset) if oldset is not None else newset
    if not fset:
        logging.warning(f'no new files found in {path}')
        return None, set()

    if verbose:
        print(f'found {len(fset)} new files in {path}')

    # max(fl2,key=getmtime)                             # 9.2us per loop, 8.1 time cache Py3.5,  # 6.2us per loop, 18 times cache  Py27
    #max((str(f) for f in flist), key=getmtime)         # 13us per loop, 20 times cache, # 10.1us per loop, no cache Py27
    newest =  max(fset, key=lambda f: f.stat().st_mtime) #14.8us per loop, 7.5times cache, # 10.3us per loop, 21 times cache Py27

    if verbose:
        print(f'newest file {newest}  {newest.stat().st_mtime}')

    return newest,newset

def spoolpath(path:Path):
    path = Path(path).expanduser()

    if path.is_dir():
        flist = sorted(path.glob('*.dat')) # list of spool files in this directory
    elif path.is_file():
        if path.suffix == '.h5': # tick file we wrote putting filename in time order
            #F = read_hdf(path,'filetick')
            with h5py.File(path,'r',libver='latest') as f:
                F = f['fn']
                P = Path(f['path'].value)
            flist = [P/f for f in F]#.values]
        else:
            flist = [path]
    else:
        raise FileNotFoundError(f'no spool files found in {path}')

    print(f'{len(flist)} files found: {path}')

    return flist

def spoolparam(inifn:Path, superx:int=None, supery:int=None, stride:int=None) -> dict:
    inifn = Path(inifn).expanduser()

    if not inifn.is_file():
        raise FileNotFoundError(f'{inifn} does not exist.')
#%% parse Solis acquisitionmetadata.ini that's autogenerated for each Kinetic series
    C = ConfigParser()
    C.read(inifn, encoding='utf-8-sig') # 'utf-8-sig' is required for Andor's weird Windows format

    Nframe = C.getint('multiimage','ImagesPerFile')

    if 'ImageSizeBytes' in C['data']: # 2016-present format
        framebytes = C.getint('data','ImageSizeBytes') #including all headers & zeros
        superx = C.getint('data','AOIWidth')
        supery = C.getint('data','AOIHeight')
        stride = C.getint('data','AOIStride')

        encoding = C.get('data','PixelEncoding')

        if encoding not in ('Mono32','Mono16'):
            logging.critical('Spool File may not be read correctly, unexpected format')

        bpp = int(encoding[-2:])
    elif 'ImageSize' in C['data']: # 2012-201? format
        framebytes = C.getint('data','ImageSize')

        # TODO arbitrary sanity check.
        if superx*supery*2 < 0.9*framebytes or superx*supery*2 > 0.999 * framebytes:
            logging.critical('unlikely this format is read correctly. Was binning/frame size different?')

        bpp = 16


    P = {'superx': superx,
         'supery': supery,
         'nframefile':Nframe,
         'stride': stride,
         'framebytes':framebytes,
         'bpp':bpp}

    return P

def readNeoSpool(fn:Path, P:dict, ifrm=None, tickonly:bool=False, zerocols:int=0):
    """
    for 2012-present Neo/Zyla sCMOS Andor Solis spool files.
    reads a SINGLE spool file and returns the image frames & FPGA ticks

    inputs:
    fn: path to specific Neo spool file .dat
    P: dict of camera data parameters
    ifrm: None (read all .dat frames), int (read single frame),  list/tuple/range/ndarray (read subset of frames)
    tickonly: for speed, only read tick (used heavily to create master time index)
    zerocols: some spool formats had whole columns of zeros

    output:
    imgs: Nimg,x,y 3-D ndarray image stack
    ticks: raw FPGA tick indices of "imgs"
    tsec: elapsed time of frames start (sec)
    """
    assert fn.suffix == '.dat', 'Need a spool file, you gave {fn}'
    #%% parse header

    nx, ny= P['superx'], P['supery']

    if P['bpp']==16: # 2013-2015ish
        dtype = np.uint16
        if zerocols>0:
            xslice = slice(None,-zerocols)
        else:
            xslice = slice(None)
    elif P['bpp']==32: # 2016-present
        dtype = np.uint32
        xslice=slice(None)
    else:
        raise NotImplementedError('unknown spool format')

    npixframe = (nx+zerocols)*ny
#%% check size of spool file
    if not P['framebytes'] == (npixframe * P['bpp']//8) + P['stride']:
        raise IOError(f'{fn} may be read incorrectly--wrong framebytes')

    filebytes = fn.stat().st_size
    if P['nframefile'] != filebytes // P['framebytes']:
        raise IOError(f'{fn} may be read incorrectly -- wrong # of frames/file')
# %% tick only jump
    if tickonly:
        with fn.open('rb') as f:
            f.seek(npixframe*dtype(0).itemsize, 0)
            tick = np.fromfile(f, dtype=np.uint64, count=P['stride']//8)[-2]
            return tick
# %% read this spool file
    if ifrm is None:
        ifrm = range(P['nframefile'])
    elif isinstance(ifrm,(int,np.int64)):
        ifrm = [ifrm]

    imgs = np.empty((len(ifrm),ny,nx), dtype=dtype)
    ticks  = np.zeros(len(ifrm), dtype=np.uint64)

    if 'kinetic' in P and P['kinetic'] is not None:
        tsec = np.empty(P['nframefile'])
        toffs = P['nfile']*P['nframefile']*P['kinetic']
    else:
        tsec = None

    bytesperframe = npixframe*dtype(0).itemsize + P['stride']//8*np.uint64(0).itemsize
    assert bytesperframe == P['framebytes']
    with fn.open('rb') as f:
        j=0
        for i in ifrm:
            f.seek(i*bytesperframe, 0)

            img = np.fromfile(f, dtype=dtype, count=npixframe).reshape((ny, nx+zerocols))

#            if (img==0).all():  # old < ~2010 Solis spool file is over
#                break

            imgs[j,...] = img[:,xslice]
# %% get FPGA ticks value (propto elapsed time)
        # NOTE see ../Matlab/parseNeoHeader.m for other numbers, which are probably useless. Use struct.unpack() with them
            ticks[j] = np.fromfile(f, dtype=np.uint64, count=P['stride']//8)[-2]

            if tsec is not None:
                tsec[j] = j*P['kinetic'] + toffs

            j+=1

    imgs = imgs[:j,...] # remove blank images Solis throws at the end sometimes.
    ticks = ticks[:j]

    return imgs, ticks, tsec


def tickfile(flist:list, P:dict, outfn:Path, zerocol:int) -> Series:
    """
    sorts filenames into FPGA tick order so that you can read video in time order
    """
# %% input checking
    assert isinstance(P, dict)
    assert isinstance(outfn,(str,Path))

    outfn = Path(outfn).expanduser()
    assert not outfn.is_dir(),'specify a filename to write, not just the directory.'

    if outfn.is_file() and outfn.suffix != '.h5':
        outfn = outfn.with_suffix('.h5')
    # yes check a second time
    if outfn.is_file() and outfn.stat().st_size > 0:
        raise IOError(f'Output tick {outfn} already exists, aborting.')
# %% sort indices
    logging.debug('ordering randomly named spool files vs. time (ticks)')

    tic = time()
    ticks = np.empty(len(flist), dtype=np.int64)  # must be int64, not int for Windows in general.
    for i,f in enumerate(flist):
        ticks[i]  = readNeoSpool(f,P,0,True,zerocol)
        if not i % 100:
            print(f'\r{i/len(flist)*100:.1f} %',end="")

    F = Series(index=ticks,data=[f.name for f in flist])
    F.sort_index(inplace=True)
    print(f'sorted {len(flist)} files vs. time ticks in {time()-tic:.1f} seconds')

# %% writing HDF5 iprintndex
    print(f'writing {outfn}')
    #  F.to_hdf(outfn, 'filetick', mode='w')
    #with h5py.File(outfn, 'a', libver='latest') as f:
    with h5py.File(outfn, 'w', libver='latest') as f:
        f['index'] = F.index
        f['fn'] = F.values
        f['path'] = str(flist[0].parent)

    return F


def annowrite(I, newfn:Path, pngfn:Path):
    pngfn = Path(pngfn).expanduser()
    pngfn.parent.mkdir(parents=True,exist_ok=True)

    if cv2:
        cv2.putText(I,
                    text=datetime.fromtimestamp(newfn.stat().st_mtime,tz=UTC).strftime('%x %X'),
                    org=(3,35),
                    fontFace=cv2.FONT_HERSHEY_SIMPLEX,
                    fontScale=1.1,
                    color=(255,255,255),
                    thickness=2)
#%% write to disk
        cv2.imwrite(str(pngfn),I) #if using color, remember opencv requires BGR color order
    else:
        imsave(pngfn, I)
# %%
def oldspool(path, xy, bn, kineticsec, startutc, outfn:Path):
    """
    Matlab Engine import can screw up sometimes, better to import only when truly needed.
    """
    try:
        import matlab.engine
    except ImportError:
        matlab = None
    """
    for old 2011 solis with defects 12 bit, big endian, little endian alternating
    """
    if not outfn:
        raise ValueError('you must specify an output file to write')

    path =  Path(path).expanduser()
    outfn = Path(outfn).expanduser()

    if path.is_file():
        flist = [path]
    elif path.is_dir():
        flist = sorted(path.glob('*.dat'))
    else:
        raise FileNotFoundError(f'no files found  {path}')

    nfile = len(flist)
    if nfile<1:
        raise FileNotFoundError(f'no files found  {path}')

    print(f'Found {nfile} .dat files in {path}')
#%% use matlab to unpack corrupt file
    if matlab:
        print('starting Matlab')
        eng = matlab.engine.start_matlab("-nojvm")  # nojvm makes vastly faster, disables plots
    else:
        raise ImportError('matlab engine not yet setup. see\n https://scivision.co/matlab-engine-callable-from-python-how-to-install-and-setup/' )

    try:
        nx,ny= xy[0]//bn[0], xy[1]//bn[1]

        with h5py.File(outfn, 'w', libver='latest') as fh5:
            fimg = setupimgh5(fh5,nfile,ny,nx)

            for i,f in enumerate(flist): # these old spool files were named sequentially... not so since 2012 or so!
                print(f'processing {f}   {i+1} / {nfile}')
                try:
                    datmat = eng.readNeoPacked12bit(str(f), nx,ny)
                    assert datmat.size == (ny,nx)
                    fimg[i,...] = datmat  # slow due to implicit casting from Matlab array to Numpy array--only way to do it.
                except AssertionError as e:
                    logging.critical(f'matlab returned improper size array {e}')
                except Exception as e:
                    logging.critical(f'matlab had a problem on frame {i}   {e}')
    finally:
        eng.quit()

    rawind = np.arange(nfile)+1
    ut1 = frame2ut1(startutc,kineticsec,rawind)

    return rawind,ut1
