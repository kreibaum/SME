"""
Spectral Synthesis Module of SME
"""
import logging
import warnings

import numpy as np
from tqdm import tqdm
from scipy.constants import speed_of_light
from scipy.ndimage.filters import convolve
from scipy.interpolate import interp1d


from . import broadening
from .atmosphere.interpolation import interp_atmo_grid
from .continuum_and_radial_velocity import match_rv_continuum
from .large_file_storage import setup_lfs
from .iliffe_vector import Iliffe_vector
from .nlte import update_nlte_coefficients
from .sme_synth import SME_DLL
from .util import safe_interpolation

logger = logging.getLogger(__name__)

clight = speed_of_light * 1e-3  # km/s


class Synthesizer:
    def __init__(self, config=None, lfs_atmo=None, lfs_nlte=None, dll=None):
        self.config, self.lfs_atmo, self.lfs_nlte = setup_lfs(
            config, lfs_atmo, lfs_nlte
        )
        self.dll = dll if dll is not None else SME_DLL()
        logger.critical("Don't forget to cite your sources. Use sme.citation()")

    def get_atmosphere(self, sme):
        """
        Return an atmosphere based on specification in an SME structure

        sme.atmo.method defines mode of action:
            "grid"
                interpolate on atmosphere grid
            "embedded"
                No change
            "routine"
                calls sme.atmo.source(sme, atmo)

        Parameters
        ---------
            sme : SME_Struct
                sme structure with sme.atmo = atmosphere specification

        Returns
        -------
        sme : SME_Struct
            sme structure with updated sme.atmo
        """

        # Handle atmosphere grid or user routine.
        atmo = sme.atmo
        func = self

        if hasattr(func, "msdi_save"):
            msdi_save = func.msdi_save
            prev_msdi = func.prev_msdi
        else:
            msdi_save = None
            prev_msdi = None

        if atmo.method == "grid":
            reload = msdi_save is None or atmo.source != prev_msdi[1]
            atmo = interp_atmo_grid(
                sme.teff, sme.logg, sme.monh, sme.atmo, self.lfs_atmo, reload=reload
            )
            prev_msdi = [atmo.method, atmo.source, atmo.depth, atmo.interp]
            setattr(func, "prev_msdi", prev_msdi)
            setattr(func, "msdi_save", True)
        elif atmo.method == "routine":
            atmo = atmo.source(sme, atmo)
        elif atmo.method == "embedded":
            # atmo structure already extracted in sme_main
            pass
        else:
            raise AttributeError("Source must be 'grid', 'routine', or 'embedded'")

        sme.atmo = atmo
        return sme

    def get_wavelengthrange(self, wran, vrad, vsini):
        """
        Determine wavelengthrange that needs to be calculated
        to include all lines within velocity shift vrad + vsini
        """
        # 30 km/s == maximum barycentric velocity
        vrad_pad = 30.0 + 0.5 * np.clip(vsini, 0, None)  # km/s
        vbeg = vrad_pad + np.clip(vrad, 0, None)  # km/s
        vend = vrad_pad - np.clip(vrad, None, 0)  # km/s

        wbeg = wran[0] * (1 - vbeg / clight)
        wend = wran[1] * (1 + vend / clight)
        return wbeg, wend

    def new_wavelength_grid(self, wint):
        """ Generate new wavelength grid within bounds of wint"""
        # Determine step size for a new model wavelength scale, which must be uniform
        # to facilitate convolution with broadening kernels. The uniform step size
        # is the larger of:
        #
        # [1] smallest wavelength step in WINT_SEG, which has variable step size
        # [2] 10% the mean dispersion of WINT_SEG
        # [3] 0.05 km/s, which is 1% the width of solar line profiles

        wbeg, wend = wint[[0, -1]]
        wmid = 0.5 * (wend + wbeg)  # midpoint of segment
        wspan = wend - wbeg  # width of segment
        diff = wint[1:] - wint[:-1]
        jmin = np.argmin(diff)
        vstep1 = diff[jmin] / wint[jmin] * clight  # smallest step
        vstep2 = 0.1 * wspan / (len(wint) - 1) / wmid * clight  # 10% mean dispersion
        vstep3 = 0.05  # 0.05 km/s step
        vstep = max(vstep1, vstep2, vstep3)  # select the largest

        # Generate model wavelength scale X, with uniform wavelength step.
        nx = int(
            np.abs(np.log10(wend / wbeg)) / np.log10(1 + vstep / clight) + 1
        )  # number of wavelengths
        if nx % 2 == 0:
            nx += 1  # force nx to be odd

        # Resolution
        # IDL way
        # resol_out = 1 / ((wend / wbeg) ** (1 / (nx - 1)) - 1)
        # vstep = clight / resol_out
        # x_seg = wbeg * (1 + 1 / resol_out) ** np.arange(nx)

        # Python way (not identical, as IDL endpoint != wend)
        # difference approx 1e-7
        x_seg = np.geomspace(wbeg, wend, num=nx)
        resol_out = 1 / np.diff(np.log(x_seg[:2]))[0]
        vstep = clight / resol_out
        return x_seg, vstep

    @staticmethod
    def check_segments(sme, segments):
        if isinstance(segments, str) and segments == "all":
            segments = range(sme.nseg)
        else:
            segments = np.atleast_1d(segments)
            if np.any(segments < 0) or np.any(segments >= sme.nseg):
                raise IndexError("Segment(s) out of range")
        return segments

    def apply_radial_velocity_and_continuum(
        self, wave, wmod, smod, cmod, vrad, cscale, segments
    ):
        for il in segments:
            if vrad[il] is not None:
                rv_factor = np.sqrt((1 + vrad[il] / clight) / (1 - vrad[il] / clight))
                wmod[il] *= rv_factor
            smod[il] = safe_interpolation(wmod[il], smod[il], wave[il])
            if cmod is not None:
                cmod[il] = safe_interpolation(wmod[il], cmod[il], wave[il])

            if cscale[il] is not None and not np.all(cscale[il] == 0):
                x = wave[il] - wave[il][0]
                smod[il] *= np.polyval(cscale[il], x)
        return smod

    def integrate_flux(self, mu, inten, deltav, vsini, vrt, osamp=1):
        """
        Produces a flux profile by integrating intensity profiles (sampled
        at various mu angles) over the visible stellar surface.

        Intensity profiles are weighted by the fraction of the projected
        stellar surface they represent, apportioning the area between
        adjacent MU points equally. Additional weights (such as those
        used in a Gauss-Legendre quadrature) can not meaningfully be
        used in this scheme.  About twice as many points are required
        with this scheme to achieve the precision of Gauss-Legendre
        quadrature.
        DELTAV, VSINI, and VRT must all be in the same units (e.g. km/s).
        If specified, OSAMP should be a positive integer.

        Parameters
        ----------
        mu : array(float) of size (nmu,)
            cosine of the angle between the outward normal and
            the line of sight for each intensity spectrum in INTEN.
        inten : array(float) of size(nmu, npts)
            intensity spectra at specified values of MU.
        deltav : float
            velocity spacing between adjacent spectrum points
            in INTEN (same units as VSINI and VRT).
        vsini : float
            maximum radial velocity, due to solid-body rotation.
        vrt : float
            radial-tangential macroturbulence parameter, i.e.
            np.sqrt(2) times the standard deviation of a Gaussian distribution
            of turbulent velocities. The same distribution function describes
            the radial motions of one component and the tangential motions of
            a second component. Each component covers half the stellar surface.
            See 'The Observation and Analysis of Stellar Photospheres', Gray.
        osamp : int, optional
            internal oversampling factor for convolutions.
            By default convolutions are done using the input points (OSAMP=1),
            but when OSAMP is set to higher integer values, the input spectra
            are first oversampled by cubic spline interpolation.

        Returns
        -------
        value : array(float) of size (npts,)
            Disk integrated flux profile.

        Note
        ------------
            If you use this algorithm in work that you publish, please cite
            Valenti & Anderson 1996, PASP, currently in preparation.
        """
        """
        History
        -----------
        Feb-88  GM
            Created ANA version.
        13-Oct-92 JAV
            Adapted from G. Marcy's ANA routi!= of the same name.
        03-Nov-93 JAV
            Switched to annular convolution technique.
        12-Nov-93 JAV
            Fixed bug. Intensity compo!=nts not added when vsini=0.
        14-Jun-94 JAV
            Reformatted for "public" release. Heavily commented.
            Pass deltav instead of 2.998d5/deltav. Added osamp
            keyword. Added rebinning logic at end of routine.
            Changed default osamp from 3 to 1.
        20-Feb-95 JAV
            Added mu as an argument to handle arbitrary mu sampling
            and remove ambiguity in intensity profile ordering.
            Interpret VTURB as np.sqrt(2)*sigma instead of just sigma.
            Replaced call_external with call to spl_{init|interp}.
        03-Apr-95 JAV
            Multiply flux by pi to give observed flux.
        24-Oct-95 JAV
            Force "nmk" padding to be at least 3 pixels.
        18-Dec-95 JAV
            Renamed from dskint() to rtint(). No longer make local
            copy of intensities. Use radial-tangential instead
            of isotropic Gaussian macroturbulence.
        26-Jan-99 JAV
            For NMU=1 and VSINI=0, assume resolved solar surface#
            apply R-T macro, but supress vsini broadening.
        01-Apr-99 GMH
            Use annuli weights, rather than assuming ==ual area.
        07-Mar-12 JAV
            Force vsini and vmac to be scalars.
        """

        # Make local copies of various input variables, which will be altered below.
        # Force vsini and especially vmac to be scalars. Otherwise mu dependence fails.

        if np.size(vsini) > 1:
            vsini = vsini[0]
        if np.size(vrt) > 1:
            vrt = vrt[0]

        # Determine oversampling factor.
        os = round(np.clip(osamp, 1, None))  # force integral value > 1

        # Convert input MU to projected radii, R, of annuli for a star of unit radius
        #  (which is just sine, rather than cosine, of the angle between the outward
        #  normal and the line of sight).
        rmu = np.sqrt(1 - mu ** 2)  # use simple trig identity

        # Sort the projected radii and corresponding intensity spectra into ascending
        #  order (i.e. from disk center to the limb), which is equivalent to sorting
        #  MU in descending order.
        isort = np.argsort(rmu)
        rmu = rmu[isort]  # reorder projected radii
        nmu = np.size(mu)  # number of radii
        if nmu == 1:
            if vsini != 0:
                logger.warning(
                    "Vsini is non-zero, but only one projected radius (mu value) is set. No rotational broadening will be performed."
                )
                vsini = 0  # ignore vsini if only 1 mu

        # Calculate projected radii for boundaries of disk integration annuli.  The n+1
        # boundaries are selected such that r(i+1) exactly bisects the area between
        # rmu(i) and rmu(i+1). The in!=rmost boundary, r(0) is set to 0 (disk center)
        # and the outermost boundary, r(nmu) is set to 1 (limb).
        if nmu > 1 or vsini != 0:  # really want disk integration
            r = np.sqrt(
                0.5 * (rmu[:-1] ** 2 + rmu[1:] ** 2)
            )  # area midpoints between rmu
            r = np.concatenate(([0], r, [1]))

            # Calculate integration weights for each disk integration annulus.  The weight
            # is just given by the relative area of each annulus, normalized such that
            # the sum of all weights is unity.  Weights for limb darkening are included
            # explicitly in the intensity profiles, so they aren't needed here.
            wt = r[1:] ** 2 - r[:-1] ** 2  # weights = relative areas
        else:
            wt = np.array([1.0])  # single mu value, full weight

        # Generate index vectors for input and oversampled points. Note that the
        # oversampled indicies are carefully chosen such that every "os" finely
        # sampled points fit exactly into one input bin. This makes it simple to
        # "integrate" the finely sampled points at the end of the routine.
        npts = inten.shape[1]  # number of points
        xpix = np.arange(npts, dtype=float)  # point indices
        nfine = os * npts  # number of oversampled points
        xfine = (0.5 / os) * (
            2 * np.arange(nfine, dtype=float) - os + 1
        )  # oversampled points indices

        # Loop through annuli, constructing and convolving with rotation kernels.

        yfine = np.empty(nfine)  # init oversampled intensities
        flux = np.zeros(nfine)  # init flux vector
        for imu in range(nmu):  # loop thru integration annuli

            #  Use external cubic spline routine (adapted from Numerical Recipes) to make
            #  an oversampled version of the intensity profile for the current annulus.
            ypix = inten[isort[imu]]  # extract intensity profile
            if os == 1:
                # just copy (use) original profile
                yfine = ypix
            else:
                # spline onto fine wavelength scale
                yfine = interp1d(xpix, ypix, kind="cubic")(xfine)

            # Construct the convolution kernel which describes the distribution of
            # rotational velocities present in the current annulus. The distribution has
            # been derived analytically for annuli of arbitrary thickness in a rigidly
            # rotating star. The kernel is constructed in two pieces: o!= piece for
            # radial velocities less than the maximum velocity along the inner edge of
            # the annulus, and one piece for velocities greater than this limit.
            if vsini > 0:
                # nontrivial case
                r1 = r[imu]  # inner edge of annulus
                r2 = r[imu + 1]  # outer edge of annulus
                dv = deltav / os  # oversampled velocity spacing
                maxv = vsini * r2  # maximum velocity in annulus
                nrk = 2 * int(maxv / dv) + 3  ## oversampled kernel point
                # velocity scale for kernel
                v = dv * (np.arange(nrk, dtype=float) - ((nrk - 1) / 2))
                rkern = np.zeros(nrk)  # init rotational kernel
                j1 = np.abs(v) < vsini * r1  # low velocity points
                rkern[j1] = np.sqrt((vsini * r2) ** 2 - v[j1] ** 2) - np.sqrt(
                    (vsini * r1) ** 2 - v[j1] ** 2
                )  # generate distribution

                j2 = (np.abs(v) >= vsini * r1) & (np.abs(v) <= vsini * r2)
                rkern[j2] = np.sqrt(
                    (vsini * r2) ** 2 - v[j2] ** 2
                )  # generate distribution

                rkern = rkern / np.sum(rkern)  # normalize kernel

                # Convolve the intensity profile with the rotational velocity kernel for this
                # annulus. Pad each end of the profile with as many points as are in the
                # convolution kernel. This reduces Fourier ringing. The convolution may also
                # be do!= with a routi!= called "externally" from IDL, which efficiently
                # shifts and adds.
                if nrk > 3:
                    yfine = convolve(yfine, rkern, mode="nearest")

            # Calculate projected sigma for radial and tangential velocity distributions.
            muval = mu[isort[imu]]  # current value of mu
            sigma = os * vrt / np.sqrt(2) / deltav  # standard deviation in points
            sigr = sigma * muval  # reduce by current mu value
            sigt = sigma * np.sqrt(1.0 - muval ** 2)  # reduce by np.sqrt(1-mu**2)

            # Figure out how many points to use in macroturbulence kernel.
            nmk = int(10 * sigma)
            nmk = np.clip(nmk, 3, (nfine - 3) // 2)

            # Construct radial macroturbulence kernel with a sigma of mu*VRT/np.sqrt(2).
            if sigr > 0:
                xarg = np.linspace(-nmk, nmk, 2 * nmk + 1) / sigr
                xarg = np.clip(-0.5 * xarg ** 2, -20, None)
                mrkern = np.exp(xarg)  # compute the gaussian
                mrkern = mrkern / np.sum(mrkern)  # normalize the profile
            else:
                mrkern = np.zeros(2 * nmk + 1)  # init with 0d0
                mrkern[nmk] = 1.0  # delta function

            # Construct tangential kernel with a sigma of np.sqrt(1-mu**2)*VRT/np.sqrt(2).
            if sigt > 0:
                xarg = np.linspace(-nmk, nmk, 2 * nmk + 1) / sigt
                xarg = np.clip(-0.5 * xarg ** 2, -20, None)
                mtkern = np.exp(xarg)  # compute the gaussian
                mtkern = mtkern / np.sum(mtkern)  # normalize the profile
            else:
                mtkern = np.zeros(2 * nmk + 1)  # init with 0d0
                mtkern[nmk] = 1.0  # delta function

            # Sum the radial and tangential components, weighted by surface area.
            area_r = 0.5  # assume equal areas
            area_t = 0.5  # ar+at must equal 1
            mkern = area_r * mrkern + area_t * mtkern  # add both components

            # Convolve the total flux profiles, again padding the spectrum on both ends to
            # protect against Fourier ringing.
            yfine = convolve(
                yfine, mkern, mode="nearest"
            )  # add the padding and convolve

            # Add contribution from current annulus to the running total.
            flux = flux + wt[imu] * yfine  # add profile to running total

        flux = np.reshape(flux, (npts, os))  # convert to an array
        flux = np.pi * np.sum(flux, axis=1) / os  # sum, normalize
        return flux

    def synthesize_spectrum(
        self,
        sme,
        segments="all",
        passLineList=True,
        passAtmosphere=True,
        passNLTE=True,
        updateStructure=True,
        updateLineList=False,
        reuse_wavelength_grid=False,
        radial_velocity_mode="robust",
    ):
        """
        Calculate the synthetic spectrum based on the parameters passed in the SME structure
        The wavelength range of each segment is set in sme.wran
        The specific wavelength grid is given by sme.wave, or is generated on the fly if sme.wave is None

        Will try to fit radial velocity RV and continuum to observed spectrum, depending on vrad_flag and cscale_flag

        Other important fields:
        sme.iptype: instrument broadening type

        Parameters
        ----------
        sme : SME_Struct
            sme structure, with all necessary parameters for the calculation
        setLineList : bool, optional
            wether to pass the linelist to the c library (default: True)
        passAtmosphere : bool, optional
            wether to pass the atmosphere to the c library (default: True)
        passNLTE : bool, optional
            wether to pass NLTE departure coefficients to the c library (default: True)
        reuse_wavelength_grid : bool, optional
            wether to use sme.wint as the output grid of the function or create a new grid (default: False)

        Returns
        -------
        sme : SME_Struct
            same sme structure with synthetic spectrum in sme.smod
        """

        # Define constants
        n_segments = sme.nseg
        cscale_degree = sme.cscale_degree

        # fix impossible input
        if "spec" not in sme:
            sme.vrad_flag = "none"
            sme.cscale_flag = "none"
        else:
            if "mask" not in sme:
                sme.mask = np.full(sme.spec.size, sme.mask_values["line"])
            for i in range(sme.nseg):
                sme.mask[i, ~np.isfinite(sme.spec[i])] = 0

        if "wint" not in sme:
            reuse_wavelength_grid = False
        if radial_velocity_mode != "robust" and (
            "cscale" not in sme or "vrad" not in sme
        ):
            radial_velocity_mode = "robust"

        segments = Synthesizer.check_segments(sme, segments)

        # Prepare arrays
        vrad = np.zeros(n_segments)
        cscale = np.zeros((n_segments, cscale_degree + 1))
        cscale[:, -1] = 1

        wave = [np.zeros(0) for _ in range(n_segments)]
        smod = [[] for _ in range(n_segments)]
        cmod = [[] for _ in range(n_segments)]
        wmod = [[] for _ in range(n_segments)]

        # If wavelengths are already defined use those as output
        if "wave" in sme:
            wave = [w for w in sme.wave]

        # Input Model data to C library
        self.dll.SetLibraryPath()
        if passLineList:
            self.dll.InputLineList(sme.linelist)
        if updateLineList:
            # TODO Currently Updates the whole linelist, could be improved to only change affected lines
            self.dll.UpdateLineList(
                sme.atomic, sme.species, np.arange(len(sme.linelist))
            )
        if passAtmosphere:
            sme = self.get_atmosphere(sme)
            self.dll.InputModel(sme.teff, sme.logg, sme.vmic, sme.atmo)
            self.dll.InputAbund(sme.abund)
            self.dll.Ionization(0)
            self.dll.SetVWscale(sme.gam6)
            self.dll.SetH2broad(sme.h2broad)
        if passNLTE:
            update_nlte_coefficients(sme, self.dll, self.lfs_nlte)

        # Loop over segments
        #   Input Wavelength range and Opacity
        #   Calculate spectral synthesis for each
        #   Interpolate onto geomspaced wavelength grid
        #   Apply instrumental and turbulence broadening

        # TODO Parallelization
        # This requires changes in the C code however, since SME uses global parameters
        # for the wavelength range (and opacities) which change within each segment
        for il in tqdm(segments, desc="Segment", leave=False):
            wmod[il], smod[il], cmod[il] = self.synthesize_segment(
                sme, il, reuse_wavelength_grid, il != segments[0]
            )

            if "wave" not in sme or len(sme.wave[il]) == 0:
                # trim padding
                wbeg, wend = sme.wran[il]
                itrim = (wmod[il] > wbeg) & (wmod[il] < wend)
                # Force endpoints == wavelength range
                wave[il] = np.concatenate(([wbeg], wmod[il][itrim], [wend]))

        if sme.specific_intensities_only:
            return wmod, smod, cmod

        # Fit continuum and radial velocity
        # And interpolate the flux onto the wavelength grid
        if radial_velocity_mode == "robust":
            cscale, cscale_unc, vrad, vrad_unc = match_rv_continuum(
                sme, segments, wmod, smod
            )
            logger.debug("Radial velocity: %s", str(vrad))
            logger.debug("Continuum coefficients: %s", str(cscale))
        elif radial_velocity_mode == "fast":
            cscale, vrad = sme.cscale, sme.vrad
        else:
            raise ValueError("Radial Velocity mode not understood")

        smod = self.apply_radial_velocity_and_continuum(
            wave, wmod, smod, cmod, vrad, cscale, segments
        )

        # Merge all segments
        # if sme already has a wavelength this should be the same
        if updateStructure:
            if "wave" not in sme:
                # TODO: what if not all segments are there?
                sme.wave = wave
            if "synth" not in sme:
                sme.synth = smod
            if "cont" not in sme:
                sme.cont = cmod

            for s in segments:
                sme.wave[s] = wave[s]
                sme.synth[s] = smod[s]
                sme.cont[s] = cmod[s]

            if sme.cscale_flag not in ["fix", "none"]:
                for s in segments:
                    sme.cscale[s] = cscale[s]
                sme.cscale_unc = cscale_unc

            sme.vrad = np.asarray(vrad)
            sme.vrad_unc = np.asarray(vrad_unc)
            sme.nlte.flags = self.dll.GetNLTEflags()
            return sme
        else:
            wave = Iliffe_vector(values=wave)
            smod = Iliffe_vector(values=smod)
            cmod = Iliffe_vector(values=cmod)
            return wave, smod, cmod

    def synthesize_segment(
        self, sme, segment, reuse_wavelength_grid=False, keep_line_opacity=False
    ):
        """Create the synthetic spectrum of a single segment

        Parameters
        ----------
        sme : SME_Struct
            The SME strcuture containing all relevant parameters
        segment : int
            the segment to synthesize
        reuse_wavelength_grid : bool
            Whether to keep the current wavelength grid for the synthesis
            or create a new one, depending on the linelist. Default: False
        keep_line_opacity : bool
            Whether to reuse existing line opacities or not. This should be
            True iff the opacities have been calculated in another segment.

        Returns
        -------
        wgrid : array of shape (npoints,)
            Wavelength grid of the synthesized spectrum
        flux : array of shape (npoints,)
            The Flux of the synthesized spectrum
        cont_flux : array of shape (npoints,)
            The continuum Flux of the synthesized spectrum
        """
        logger.debug("Segment %i", segment)

        # Input Wavelength range and Opacity
        vrad_seg = sme.vrad[segment] if sme.vrad[segment] is not None else 0
        wbeg, wend = self.get_wavelengthrange(sme.wran[segment], vrad_seg, sme.vsini)

        self.dll.InputWaveRange(wbeg, wend)
        self.dll.Opacity()

        # Reuse adaptive wavelength grid in the jacobians
        wint_seg = sme.wint[segment] if reuse_wavelength_grid else None
        # Only calculate line opacities in the first segment
        #   Calculate spectral synthesis for each
        _, wint, sint, cint = self.dll.Transf(
            sme.mu,
            sme.accrt,  # threshold line opacity / cont opacity
            sme.accwi,
            keep_lineop=keep_line_opacity,
            wave=wint_seg,
        )

        if not sme.specific_intensities_only:
            # Create new geomspaced wavelength grid, to be used for intermediary steps
            wgrid, vstep = self.new_wavelength_grid(wint)

            # Radiative Transfer Integration
            # Continuum
            cint = self.integrate_flux(sme.mu, cint, 1, 0, 0)
            cint = np.interp(wgrid, wint, cint)

            # Broaden Spectrum
            y_integrated = np.empty((sme.nmu, len(wgrid)))
            for imu in range(sme.nmu):
                y_integrated[imu] = np.interp(wgrid, wint, sint[imu])

            # Turbulence broadening
            # Apply macroturbulent and rotational broadening while integrating intensities
            # over the stellar disk to produce flux spectrum Y.
            sint = self.integrate_flux(sme.mu, y_integrated, vstep, sme.vsini, sme.vmac)
            wint = wgrid

        # instrument broadening
        if "iptype" in sme:
            ipres = sme.ipres if np.size(sme.ipres) == 1 else sme.ipres[segment]
            sint = broadening.apply_broadening(
                ipres, wint, sint, type=sme.iptype, sme=sme
            )

        # Divide calculated spectrum by continuum
        if sme.normalize_by_continuum:
            sint /= cint

        return wint, sint, cint


def synthesize_spectrum(sme, segments="all"):
    synthesizer = Synthesizer()
    return synthesizer.synthesize_spectrum(sme, segments)
