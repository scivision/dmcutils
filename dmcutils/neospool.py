#!/usr/bin/env python
from pathlib import Path
import logging
from configparser import ConfigParser
from datetime import datetime
from pytz import UTC
import numpy as np
from scipy.misc import bytescale,imsave
import h5py
try:
    import cv2
except ImportError:
    cv2=None  #fall back to scipy imsave, no time annotation

try:
    import matlab.engine
except ImportError:
    matlab = None
#
from histutils.timedmc import frame2ut1


DTYPE = np.uint16

def findnewest(path):
    assert path, f'{path} is empty'
    path = Path(path).expanduser()
    assert path.exists(),f'{path}: could not find'
#%% it's a file
    if path.is_file():
        return path
#%% it's a directory
    flist = path.glob('*.dat')
    assert flist, f'no files found in {path}'

    # max(fl2,key=getmtime)                             # 9.2us per loop, 8.1 time cache Py3.5,  # 6.2us per loop, 18 times cache  Py27
    #max((str(f) for f in flist), key=getmtime)         # 13us per loop, 20 times cache, # 10.1us per loop, no cache Py27
    return max(flist, key=lambda f: f.stat().st_mtime) #14.8us per loop, 7.5times cache, # 10.3us per loop, 21 times cache Py27

def _spoolframesize(inifn,nxy=(640,540),stride=1296):
#%% parse Solis acquisitionmetadata.ini that's autogenerated for each Kinetic series
    C = ConfigParser()
    C.read(inifn,encoding='utf-8-sig')

    Nframe = C.getint('multiimage','ImagesPerFile')

    if 'ImageSizeBytes' in C['data']: # 2016-present format
        framebytes = C.getint('data','ImageSizeBytes') #including all headers & zeros
        nxy = (C.getint('data','AOIWidth'),C.getint('data','AOIHeight'))
        stride = C.getint('AOIStride')

        encoding = C.get('data','PixelEncoding')

        if encoding not in ('Mono32','Mono16'):
            logging.critical('Spool File may not be read correctly, unexpected format')
    elif 'ImageSize' in C['data']: # 2012-201? format
        framebytes = C.getint('data','ImageSize')

        logging.warning('Nxy,stride are hard coded for specific DMC experiment!!')

        # TODO arbitrary sanity check.
        if nxy[0]*nxy[1]*2 < 0.9*framebytes or nxy[0]*nxy[1]*2 > 0.999 * framebytes:
            logging.critical('unlikely this format is read correctly. Was binning/frame size different?')


    return nxy,Nframe,stride,framebytes

def readNeoSpool(fn,inifn,zerorows=8):
    """
    for 2012-present Neo/Zyla sCMOS Andor Solis spool files.
    reads a SINGLE spool file and returns the image frames & FPGA ticks
    """
    #%% parse header
    nxy,Nframe,stridebytes,framebytes = _spoolframesize(fn.parent/inifn)
    """ 16 bit """
    nx,ny=nxy
    npixframe = (nx+zerorows)*ny
#%% check size of spool file
    assert framebytes == (npixframe * DTYPE(0).itemsize) + stridebytes
    filebytes = fn.stat().st_size
    if Nframe != filebytes // framebytes:
        logging.critical('file may be read incorrectly')
    else:
        logging.info(f'{Nframe} frames / file')
#%% read this spool file
    imgs = np.empty((Nframe,ny,nx), dtype=DTYPE)
    ticks  = np.zeros(Nframe, dtype=np.uint64)
    with fn.open('rb') as f:
        j=0
        for i in range(Nframe):
            img = np.fromfile(f, dtype=DTYPE, count=npixframe).reshape((ny,nx+zerorows))
            if not (img==0).all():
                imgs[j,...] = img[:,:-zerorows]
#%% get FPGA ticks value (propto elapsed time)
            # NOTE see ../Matlab/parseNeoHeader.m for other numbers, which are probably useless. Use struct.unpack() with them
                ticks[j] = np.fromfile(f, dtype=np.uint64, count=stridebytes//8)[-2]

                j+=1
            else: # file is over, rest will be all zeros from my experience
                break

    imgs = imgs[:j,...] # remove blank images Solis throws at the end sometimes.
    ticks = ticks[:j]

    return imgs,ticks

def mean16to8(I):
    #%% take mean and scale images
    fmean = I.mean(axis=0)
    l,h = np.percentile(fmean,(0.5,99.5))
#%% 16 bit to 8 bit using scikit-image
    return bytescale(fmean,cmin=l,cmax=h)

def annowrite(I,newfn,pngfn):
    pngfn = Path(pngfn).expanduser()
    pngfn.parent.mkdir(parents=True,exist_ok=True)

    if cv2:
        cv2.putText(I, text=datetime.fromtimestamp(newfn.stat().st_mtime,tz=UTC).strftime('%x %X'), org=(3,35),
            fontFace=cv2.FONT_HERSHEY_SIMPLEX, fontScale=1.1,
            color=(255,255,255), thickness=2)
#%% write to disk
        cv2.imwrite(str(pngfn),I) #if using color, remember opencv requires BGR color order
    else:
        imsave(str(pngfn),I)

def oldspool(path,xy,bn,kineticsec,startutc,outfn):
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
#%%
    if matlab:
        print('starting Matlab')
        eng = matlab.engine.start_matlab("-nojvm")
    else:
        raise ImportError('matlab engine not yet setup. see\n https://scivision.co/matlab-engine-callable-from-python-how-to-install-and-setup/' )

    try:
        nx,ny= xy[0]//bn[0], xy[1]//bn[1]

        with h5py.File(str(outfn),'w',libver='latest') as fh5:
            fimg = fh5.create_dataset('/rawimg',(nfile,ny,nx),
                                      dtype=np.int16,
                                      compression='gzip',
                                      compression_opts=4,
                                      track_times=True)
            fimg.attrs["CLASS"] = np.string_("IMAGE")
            fimg.attrs["IMAGE_VERSION"] = np.string_("1.2")
            fimg.attrs["IMAGE_SUBCLASS"] = np.string_("IMAGE_GRAYSCALE")
            fimg.attrs["DISPLAY_ORIGIN"] = np.string_("LL")
            fimg.attrs['IMAGE_WHITE_IS_ZERO'] = np.uint8(0)

            for i,f in enumerate(flist):
                print(f'processing {f}   {i+1} / {nfile}')
                try:
                    datmat = eng.readNeoPacked12bit(str(f), nx,ny)
                    assert datmat.size == (ny,nx)
                    fimg[i,...] = datmat
                except AssertionError as e:
                    logging.critical(f'matlab returned improper size array {e}')
                except Exception as e:
                    logging.critical(f'matlab had a problem on frame {i}   {e}')
    finally:
        eng.quit()

    rawind = np.arange(nfile)+1
    ut1 = frame2ut1(startutc,kineticsec,rawind)

    return rawind,ut1


def h5toh5(fn,kineticsec,startutc):
    fn = Path(fn).expanduser()
    with h5py.File(str(fn),'r',libver='latest') as f:
        data = f['/rawimg']

        rawind = np.arange(data.shape[0])+1
    ut1 = frame2ut1(startutc,kineticsec,rawind)

    return rawind,ut1
