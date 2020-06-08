# Reliability estimator and direction-dependent source tagging functions tools



import matplotlib
matplotlib.use('Agg')
import Tigger
import pyfits
import numpy
import subprocess
import tempfile
import os 
import sys
import logging
import pylab
from scipy.ndimage import filters
from astLib.astWCS import WCS
import math
from scipy import stats


matplotlib.rcParams.update({'font.size': 12})

# provides logging
def logger(level=0, prefix=None):
    
    if not prefix:
        logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')
    else:
        name = prefix + ".log"
        logging.basicConfig(filename=name)  

    LOGL = {"0": "INFO",
            "1": "DEBUG",
            "2": "ERROR",
            "3": "CRITICAL"}

    log = logging.getLogger(" Sourcery ")
    log.setLevel(eval("logging."+LOGL[str(level)]))

    return log



# Reshapes the data
#-----------------------knicked from Stats.py------------------------------- 
def reshape_data (image, prefix=None):

    """ Reshape FITS data to (stokes,freq,npix_ra,npix_dec).

    Returns reshaped data, wcs, the image header, and
    pixel size.
         
    image: Fits data  
    """
    
    with pyfits.open(image) as hdu:
        data = hdu[0].data
        hdr = hdu[0].header
        shape = list(data.shape)
        ndim = len(shape)

    wcs = WCS(hdr, mode="pyfits")
    log = logger(level=0, prefix=prefix)

    pixel_size = abs(hdr["CDELT1"])
    if ndim < 2:
        log.error(" The FITS file needs at least two dimensions")
        

 # This is the shape I want the data in
    want = (
            ["STOKES",0],
            ["FREQ",1],
            ["RA",2],
            ["DEC",3],
)
   
    # Assume RA,DEC is first (FITS) or last two (NUMPY)
    if ndim > 3:
        for ctype, ind in want[:2]:
            for axis in range(1, ndim+1):
                if hdr["CTYPE%d"%axis].startswith(ctype):
                    want[ind].append(ndim-axis)
        if want[0][-1] == want[1][-2] and want[0][-2] == want[1][-1]:
            tmp = shape[0]
            shape[0] = shape[1]
            shape[1] = tmp
            data = numpy.reshape(data,shape)
    if ndim == 3:
        if not hdr["CTYPE3"].startswith("FREQ"):
            data = data[0,...]
    elif ndim > 4:
        log.error(" FITS file has more than 4 axes. Aborting")
 
    return data, wcs, hdr, pixel_size



def image_data(data, prefix=None):
    """ returns first image slice of data """

    log = logger(level=0, prefix=prefix)
    imslice = numpy.zeros(data.ndim, dtype=int).tolist()
    imslice[-1] = slice(None)
    imslice[-2] = slice(None)

    return data[imslice]
    

# computes the negative noise.
def negative_noise(data, prefix=None):

    """ Computes the image noise using the negative pixels """
    if isinstance(data, str):
        with pyfits.open(data) as hdu:
            data = hdu[0].data

    data = image_data(data, prefix)
    negative = data[data<0].flatten()
    noise = numpy.concatenate([negative,-negative]).std()
    image_mean = numpy.concatenate([negative,-negative]).mean()
    return noise, image_mean


# inverts the image
def invert_image(imagename, output, prefix=None):

    log = logger(level=0, prefix=prefix)
    log.info(" We are now creating an inverted image and saving it to %s"%output)

    with pyfits.open(imagename) as hdu:
        data = -hdu[0].data
        hdu[0].data = data
        hdu.writeto(output, clobber=True)

#----------------------------------------------------
#knicked from Sofia reliability estimator
class gaussian_kde_set_covariance(stats.gaussian_kde):
    def __init__(self, dataset, covariance):
        self.covariance = covariance
        stats.gaussian_kde.__init__(self, dataset)
    def _compute_covariance(self):
        #if numpy.linalg.det(self.covariance) != 0:
        self.inv_cov = pylab.linalg.inv(self.covariance)
        self._norm_factor = numpy.sqrt(
             pylab.linalg.det(2*numpy.pi*self.covariance)) * self.n
#---------------------------------------------------------------------------


# takes the extension of a file
def fits_ext(fitsname):
    ext = fitsname.split(".")[-1]
    return ext


# Does the smoothing
def thresh_mask(imagename, output, thresh, 
                noise=None, sigma=False, smooth=None,
                prefix=None, savemask=False):
    """ Create a threshhold mask """


    hdu = pyfits.open(imagename)

    hdr = hdu[0].header
    ndim = hdr["NAXIS"] 
    data = hdu[0].data
    nn = ndim - 2 # get number of non-image axes
    kernel = numpy.ones(ndim, dtype=int).tolist()
    
    log = logger(level=0, prefix=prefix)
    # If smooth is not specified, use a fraction of the beam
    
    if sigma:
        noise = noise or negative_noise(data)[0]
    else:
        noise = 1

    thresh = thresh * noise
    
    mask = numpy.ones(data.shape)

    if smooth:
        log.info(" A masking threshold was set to %.2f"%(thresh/noise))
        emin = hdr["BMIN"]
        emaj = hdr["BMAJ"]
        cell = abs(hdr["CDELT1"])
        beam = math.sqrt(emin*emaj)/cell
        scales = [0.1, 2.0, 5.0, 10.0, 20.0, 40.0]#, 60.0]
        smooth = None
        for scale in scales: 

            # Updating kernel
            kk = scale * beam
            kernel[-1] = kk
            kernel[-2] = kk

            smooth = filters.gaussian_filter(
                       data if smooth is None else 
                       smooth, kernel)

            mask *= smooth < thresh
    else:
        log.info(" No smoothing was made since the smooth was set to None")
        mask = data < thresh
    
    hdu[0].data *= (mask==False)

    hdu.writeto(output, clobber=True)

    if savemask:
        log.info(" Saving Masked images.")
        mask = (mask== False) * 1 
        ext = fits_ext(imagename)
        outmask = prefix + "-mask.fits" or  imagename.replace(ext,"-mask.fits")
        
        hdu[0].data = mask 

        hdu.writeto(outmask, clobber=True)
        log.info(" Masking an image %s was succesfull"%imagename)

    return mask==False, noise



# source extraction. Works with Gaul in ,fits form
def sources_extraction(image, output=None,
                       sourcefinder_name="pybdsm",
                       prefix=None, **kw):


    """Runs pybdsm on the specified 'image', converts the 
       results into a Tigger model and writes it to output.

    image :  Fits image data
    ou
        A Catalog name to store the extracted sources
    """
    # start with default PYBDSM options
    opts = {}
    opts.update(kw)

    catalogue_format = output.split(".")[-2]
    log = logger(level=0, prefix=prefix)     
    log.info(" Source finding begins...")
    if sourcefinder_name.lower() == "pybdsm":
        from lofar import bdsm
        img = bdsm.process_image(image, group_by_isl=True, **kw) 
        img.write_catalog(outfile=output, format="fits", 
                          catalog_type=catalogue_format, clobber=True)

    log.info(" Source finding was succesfully performed.")
    return output



# computes the locala variance
def compute_local_variance(imagedata, pos, step):
     
    x, y = pos   
    subrgn = imagedata[abs(y-step) : y+step, abs(x-step) : x+step]
    subrgn = subrgn[subrgn > 0]
    std = subrgn.std()
        
    return std


# plots for local variance but yet put to work.
def plot_local_variance(modellsm, noise, prefix, threshold):

    model = Tigger.load(modellsm, verbose=False)
    savefig = prefixx + "_variance.png"
    local = [(src.l/1.0e-6) for src in model.sources]
    pylab.figure()
    pylab.plot([noise/1.0e-6] * len(local))
    x = numpy.arange(len(local))
    pylab.plot(x, local)

    for i, src in enumerate(model.sources):
        if local[i] > threshold * noise:
            pylab.plot(x[i], local[i], "rD")
            pylab.annotate(src.name, xy=(x[i], local[i]))

    pylab.ylabel("Local variance[$\mu$]")
    pylab.savefig(savefig)



#computes the correlation factor
def compute_psf_correlation(image, psf, pos,
                            step=None):

    """Computes PSF correlation.
 
    model: Takes a sky model
    src: a source in question
    imagedata:  Takes image data already in 2 * 2.
    
    """   

    with pyfits.open(image) as hdu:
        imagedata = image_data(hdu[0].data)
    
    with pyfits.open(psf) as hdu:
        psfdata = image_data(hdu[0].data)
        psfhdr = hdu[0].header

    c0 = psfhdr["CRPIX2"] 
    psf_region  = psfdata[c0-step: c0+step, c0-step : c0+step].flatten()

    ra0, dec0 = pos  

    data_region = imagedata[abs(dec0-step) : dec0+step, abs(ra0-step):ra0+step].flatten()  
    norm_data = (data_region-data_region.min())/(data_region.max()-
                                                 data_region.min())
     
    return norm_data, psf_region


# plots the parameter space
def plot(pos, neg, rel=None, labels=None, show=False, savefig=None,
         prefix=None):

    log = logger(level=0, prefix=prefix)
    log.info(" Making Reliability plots ")
 
    if not savefig:
        return

    # labels for projections
    from scipy.interpolate import griddata
    plots = []
    nplanes = len(labels)
    for i, label_i in enumerate(labels):
        for label_j in labels.keys()[i+1:]:
            i = labels[label_i][0]
            j = labels[label_j][0]
            plots.append( [i, j, label_i, label_j] )

    
    npos, nneg = len(pos), len(neg)
    pylab.figure(figsize=(8*nplanes, 10*nplanes))

    if nneg < 5:
        log.error(" Few number of detections cant proceed plotting."
                  " Aborting")        
        return 

    ##TODO: plots semi automated
    if nplanes %2.0 == 0:
        column  = nplanes/2.0
        row = (nplanes-1.0)
    else:
        column =  (nplanes - 1.0)/2.0
        row = nplanes
    column_fix = column
    if  row > 5.0:
        row = column 
        column = column_fix + 1

    for counter, (i, j, x, y) in enumerate(plots):

        pylab.subplot(int(row), int(column), counter+1)
        a,b = neg[:, i], neg[:, j]
        c,d = pos[:, i], pos[:, j]

        kernel = numpy.array([a.std(), b.std()])
        cov = numpy.array([(kernel[0]**2, 0.0),(0.0, kernel[1]**2)])*\
                         ((4.0/((nplanes+2)*nneg))**(1.0/(nplanes+4.0)))
        ncov = gaussian_kde_set_covariance(numpy.array([a, b]), cov)
        
        # define axis limits for plots
        ac = numpy.concatenate((a, c))        
        bd = numpy.concatenate((b, d))        
        pylab.xlim(ac.min(), ac.max())
        pylab.ylim(bd.min(), bd.max())

        #negative detection density field
        PN = ncov(numpy.array([a, b])) * nneg
       
        xi = numpy.linspace(ac.min() ,ac.max(), 100)
        yi = numpy.linspace(bd.min(), bd.max(), 100)
        zzz = griddata((a, b), PN,(xi[None,:], yi[:,None]), method="cubic")
        pylab.tick_params(axis='x', labelsize=30)
        pylab.tick_params(axis='y', labelsize=30)
        pylab.contour(xi, yi, zzz, 20, linewidths=4, colors='c') 
        pylab.scatter(pos[:,i], pos[:,j], marker="o", c='r', s=35)
        pylab.xlabel(labels[x][1], fontsize="35")
        pylab.ylabel(labels[y][1], fontsize="35")
        pylab.grid()
    pylab.savefig(savefig)


def xrun(command, options, log=None):
    """
        Run something on command line.
        Example: _run("ls", ["-lrt", "../"])
    """

    options = map(str, options)

    cmd = " ".join([command]+options)

    if log:
        log.info("Running: %s"%cmd)
    else:
        print('running: %s'%cmd)

    process = subprocess.Popen(cmd,
                  stderr=subprocess.PIPE if not isinstance(sys.stderr,file) else sys.stderr,
                  stdout=subprocess.PIPE if not isinstance(sys.stdout,file) else sys.stdout,
                  shell=True)

    if process.stdout or process.stderr:

        out, err = process.comunicate()
        sys.stdout.write(out)
        sys.stderr.write(err)
        return out
    else:
        process.wait()
    if process.returncode:
         raise SystemError('%s: returns errr code %d'%(command, process.returncode))

     
