from __future__ import division,absolute_import
from pathlib2 import Path
import logging
from pandas import read_csv
from datetime import datetime
from pytz import UTC
from numpy import uint16,uint64,fromfile,empty,percentile,arange,string_,uint8
from scipy.misc import bytescale,imsave
from os import makedirs
import h5py
try:
    import cv2
except:
    pass  #fall back to scipy imsave, no time annotation
#
try:
    from histutils.timedmc import frame2ut1
except ImportError:
    pass #time functions won't work, that's OK for basic usage of images only.


datatype=uint16

def findnewest(path):
    assert path, '{} is empty'.format(path)
    path = Path(path).expanduser()
    assert path.exists(),'{} could not find'.format(path)
#%% it's a file
    if path.is_file():
        return path
#%% it's a directory
    flist = path.glob('*.dat')
    assert flist, 'no files found in {}'.format(path)

    # max(fl2,key=getmtime)                             # 9.2us per loop, 8.1 time cache Py3.5,  # 6.2us per loop, 18 times cache  Py27
    #max((str(f) for f in flist), key=getmtime)         # 13us per loop, 20 times cache, # 10.1us per loop, no cache Py27
    return max(flist, key=lambda f: f.stat().st_mtime) #14.8us per loop, 7.5times cache, # 10.3us per loop, 21 times cache Py27

def getframesize(path,inifn):
    if path.is_dir():
        inifn = path/inifn
    elif path.is_file():
        inifn = path.parent/inifn

    H = read_csv(str(inifn),delimiter='=',comment='[',index_col=0).iloc[:,0].rename(index=lambda x: x.strip())
    nxy  =  (int(H['AOIWidth']),int(H['AOIHeight']))
    Nframe = int(H['ImagesPerFile'])
    stride = int(H['AOIStride'])
    framebytes=int(H['ImageSizeBytes']) #including all headers & zeros


    if H['PixelEncoding'].strip().lower() != 'mono16':
        logging.warning('this program is only made for Mono16 pixel type, images may appear scrambled')

    return nxy,Nframe,stride,framebytes

def readNeoSpool(fn,inifn,zerorows=8):
    #%% parse header
    nxy,Nframe,stridebytes,framebytes = getframesize(fn,inifn)
    """ 16 bit """
    nx,ny=nxy
#    nimg   = nx * ny
    npixframe = (nx+zerorows)*ny

    assert framebytes == (npixframe * datatype(0).itemsize) + stridebytes
    filebytes = fn.stat().st_size
    if Nframe != filebytes // framebytes:
        logging.warning('file may be read incorrectly')
    else:
        logging.info('{} frames / file'.format(Nframe))

    frames = empty((Nframe,ny,nx),dtype=datatype)
    ticks  = empty(Nframe,dtype=uint64)
    with open(str(fn),'rb') as f: #FIXME: for Python 2.7 Numpy 1.10 bug with io.BufferedReader IOError
        for i in range(Nframe):
            frame = fromfile(f,dtype=uint16,count=npixframe).reshape((ny,nx+zerorows))
            frames[i,...] = frame[:,:-zerorows]

            #NOTE see histutils/parseNeoHeader.m for other numbers, which are probably useless. Use struct.unpack() with them
            ticks[i] = fromfile(f,dtype=uint64,count=stridebytes//8)[-2]
    return frames,ticks

def mean16to8(I):
    #%% take mean and scale images
    fmean = I.mean(axis=0)
    l,h = percentile(fmean,(0.5,99.5))
#%% 16 bit to 8 bit using scikit-image
    return bytescale(fmean,cmin=l,cmax=h)

def annowrite(I,newfn,pngfn):
    pngfn = Path(pngfn).expanduser()
    makedirs(str(pngfn.parent),exist_ok=True)

    try:
        cv2.putText(I, text=datetime.fromtimestamp(newfn.stat().st_mtime,tz=UTC).strftime('%x %X'), org=(3,35),
            fontFace=cv2.FONT_HERSHEY_SIMPLEX, fontScale=1.2,
            color=(255,255,255), thickness=2)
#%% write to disk
        cv2.imwrite(str(pngfn),I) #if using color, remember opencv requires BGR color order
    except NameError:
        imsave(str(pngfn),I)

def oldspool(path,xy,bn,kineticsec,startutc,outfn):
    """
    for old 2011 solis with defects 12 bit, big endian, little endian alternating
    """
    print('starting Matlab')
    import matlab.engine
    eng = matlab.engine.start_matlab("-nojvm")

    path =  Path(path).expanduser()
    outfn = Path(outfn).expanduser()
    flist = sorted(path.glob('*.dat'))
    nfile = len(flist)
    print('Found {} .dat files in {}'.format(nfile,path))

    nx,ny= xy[0]//bn[0], xy[1]//bn[1]

    with h5py.File(str(outfn),'w',libver='latest') as fh5:
        fimg = fh5.create_dataset('/rawimg',(nfile,ny,nx),
                                  dtype=uint16,
                                  compression='gzip',
                                  compression_opts=4,
                                  track_times=True)
        fimg.attrs["CLASS"] = string_("IMAGE")
        fimg.attrs["IMAGE_VERSION"] = string_("1.2")
        fimg.attrs["IMAGE_SUBCLASS"] = string_("IMAGE_GRAYSCALE")
        fimg.attrs["DISPLAY_ORIGIN"] = string_("LL")
        fimg.attrs['IMAGE_WHITE_IS_ZERO'] = uint8(0)

        for i,f in enumerate(flist):
            print('processing {}   {} / {}'.format(f,i+1,nfile))
            try:
                datmat = eng.readNeoPacked12bit(f, nx,ny)
                assert datmat.size == (ny,nx)
                fimg[i,...] = datmat
            except AssertionError as e:
                logging.critical('matlab returned improper size array {}'.format(e))
            except Exception as e:
                logging.critical('matlab had a problem on frame {}   {}'.format(i,e))

    eng.quit()

    rawind = arange(nfile)+1
    ut1 = frame2ut1(startutc,kineticsec,rawind)

    return rawind,ut1


def h5toh5(fn,kineticsec,startutc):
    fn = Path(fn).expanduser()
    with h5py.File(str(fn),'r',libver='latest') as f:
        data = f['/rawimg']

        rawind = arange(data.shape[0])+1
    ut1 = frame2ut1(startutc,kineticsec,rawind)

    return rawind,ut1