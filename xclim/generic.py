# -*- coding: utf-8 -*-
# Note: stats.dist.shapes: comma separated names of shape parameters
# The other parameters, common to all distribution, are loc and scale.
from typing import Sequence
from typing import Union

import dask.array
import numpy as np
import xarray as xr


def select_time(da: xr.DataArray, **indexer):
    """Select entries according to a time period.

    Parameters
    ----------
    da : xr.DataArray
      Input data.
    **indexer : {dim: indexer, }, optional
      Time attribute and values over which to subset the array. For example, use season='DJF' to select winter values,
      month=1 to select January, or month=[6,7,8] to select summer months. If not indexer is given, all values are
      considered.

    Returns
    -------
    xr.DataArray
      Selected input values.
    """
    if not indexer:
        selected = da
    else:
        key, val = indexer.popitem()
        time_att = getattr(da.time.dt, key)
        selected = da.sel(time=time_att.isin(val)).dropna(dim="time")

    return selected


def select_resample_op(*, da: xr.DataArray, op, freq: str = "YS", **indexer):
    """Apply operation over each period that is part of the index selection.

    Parameters
    ----------
    da : xr.DataArray
      Input data.
    op : str {'min', 'max', 'mean', 'std', 'var', 'count', 'sum', 'argmax', 'argmin'} or func
      Reduce operation. Can either be a DataArray method or a function that can be applied to a DataArray.
    freq : str
      Resampling frequency defining the periods
      defined in http://pandas.pydata.org/pandas-docs/stable/timeseries.html#resampling.
    **indexer : {dim: indexer, }, optional
      Time attribute and values over which to subset the array. For example, use season='DJF' to select winter values,
      month=1 to select January, or month=[6,7,8] to select summer months. If not indexer is given, all values are
      considered.

    Returns
    -------
    xarray.DataArray
      The maximum value for each period.
    """
    da = select_time(da, **indexer)
    r = da.resample(time=freq, keep_attrs=True)
    if isinstance(op, str):
        return getattr(r, op)(dim="time", keep_attrs=True)

    return r.apply(op)


def doymax(da: xr.DataArray):
    """Return the day of year of the maximum value."""
    i = da.argmax(dim="time")
    out = da.time.dt.dayofyear[i]
    out.attrs["units"] = ""
    return out


def doymin(da: xr.DataArray):
    """Return the day of year of the minimum value."""
    i = da.argmax(dim="time")
    out = da.time.dt.dayofyear[i]
    out.attrs["units"] = ""
    return out


def fit(da: xr.DataArray, dist: str = "norm"):
    """Fit an array to a univariate distribution along the time dimension.

    Parameters
    ----------
    da : xr.DataArray
      Time series to be fitted along the time dimension.
    dist : str
      Name of the univariate distribution, such as beta, expon, genextreme, gamma, gumbel_r, lognorm, norm
      (see scipy.stats).

    Returns
    -------
    xr.DataArray
      An array of distribution parameters fitted using the method of Maximum Likelihood.

    Notes
    -----
    Coordinates for which all values are NaNs will be dropped before fitting the distribution. If the array
    still contains NaNs, the distribution parameters will be returned as NaNs.
    """
    # Get the distribution
    dc = get_dist(dist)
    shape_params = [] if dc.shapes is None else dc.shapes.split(",")
    dist_params = shape_params + ["loc", "scale"]

    # Fit the parameters.
    # This would also be the place to impose constraints on the series minimum length if needed.
    def fitfunc(arr):
        """Fit distribution parameters."""
        x = np.ma.masked_invalid(arr).compressed()

        # Return NaNs if array is empty.
        if len(x) <= 1:
            return [np.nan] * len(dist_params)

        # Fill with NaNs if one of the parameters is NaN
        params = dc.fit(x)
        if np.isnan(params).any():
            params[:] = np.nan

        return params

    # xarray.apply_ufunc does not yet support multiple outputs with dask parallelism.
    data = dask.array.apply_along_axis(fitfunc, da.get_axis_num("time"), da)

    # Count the number of values used for the fit.
    # n = da.notnull().count(dim='time')

    # Coordinates for the distribution parameters
    coords = dict(da.coords.items())
    coords.pop("time")
    coords["dparams"] = dist_params

    # Dimensions for the distribution parameters
    dims = ["dparams"]
    dims.extend(da.dims)
    dims.remove("time")

    out = xr.DataArray(data=data, coords=coords, dims=dims)
    out.attrs = da.attrs
    out.attrs["original_name"] = getattr(da, "standard_name", "")
    out.attrs[
        "description"
    ] = f"Parameters of the {dist} distribution fitted over {getattr(da, 'standard_name', '')}"
    out.attrs["estimator"] = "Maximum likelihood"
    out.attrs["scipy_dist"] = dist
    out.attrs["units"] = ""
    # out.name = 'params'
    return out


def fa(
    da: xr.DataArray, t: Union[int, Sequence], dist: str = "norm", mode: str = "high"
):
    """Return the value corresponding to the given return period.

    Parameters
    ----------
    da : xr.DataArray
      Maximized/minimized input data with a `time` dimension.
    t : Union[int, Sequence]
      Return period. The period depends on the resolution of the input data. If the input array's resolution is
      yearly, then the return period is in years.
    dist : str
      Name of the univariate distribution, such as beta, expon, genextreme, gamma, gumbel_r, lognorm, norm
      (see scipy.stats).
    mode : {'min', 'max}
      Whether we are looking for a probability of exceedance (max) or a probability of non-exceedance (min).

    Returns
    -------
    xarray.DataArray
      An array of values with a 1/t probability of exceedance (if mode=='max').
    """
    t = np.atleast_1d(t)

    # Get the distribution
    dc = get_dist(dist)

    # Fit the parameters of the distribution
    p = fit(da, dist)

    # Create a lambda function to facilitate passing arguments to dask. There is probably a better way to do this.
    if mode in ["max", "high"]:

        def func(x):
            return dc.isf(1.0 / t, *x)

    elif mode in ["min", "low"]:

        def func(x):
            return dc.ppf(1.0 / t, *x)

    else:
        raise ValueError(f"Mode `{mode}` should be either 'max' or 'min'.")

    data = dask.array.apply_along_axis(func, p.get_axis_num("dparams"), p)

    # Create coordinate for the return periods
    coords = dict(p.coords.items())
    coords.pop("dparams")
    coords["return_period"] = t

    # Create dimensions
    dims = list(p.dims)
    dims.remove("dparams")
    dims.insert(0, "return_period")

    # TODO: add time and time_bnds coordinates (Low will work on this)
    # time.attrs['climatology'] = 'climatology_bounds'
    # coords['time'] =
    # coords['climatology_bounds'] =

    out = xr.DataArray(data=data, coords=coords, dims=dims)
    out.attrs = p.attrs
    out.attrs["standard_name"] = f"{dist} quantiles"
    out.attrs[
        "long_name"
    ] = f"{dist} return period values for {getattr(da, 'standard_name', '')}"
    out.attrs["cell_methods"] = (
        out.attrs.get("cell_methods", "") + " dparams: ppf"
    ).strip()
    out.attrs["units"] = da.attrs.get("units", "")
    out.attrs["mode"] = mode
    out.attrs["history"] = (
        out.attrs.get("history", "") + "Compute values corresponding to return periods."
    )

    return out


def frequency_analysis(da, mode, t, dist, window=1, freq=None, **indexer):
    """Return the value corresponding to a return period.

    Parameters
    ----------
    da : xarray.DataArray
      Input data.
    t : int or sequence
      Return period. The period depends on the resolution of the input data. If the input array's resolution is
      yearly, then the return period is in years.
    dist : str
      Name of the univariate distribution, such as beta, expon, genextreme, gamma, gumbel_r, lognorm, norm
      (see scipy.stats).
    mode : {'min', 'max'}
      Whether we are looking for a probability of exceedance (high) or a probability of non-exceedance (low).
    window : int
      Averaging window length (days).
    freq : str
      Resampling frequency. If None, the frequency is assumed to be 'YS' unless the indexer is season='DJF',
      in which case `freq` would be set to `YS-DEC`.
    **indexer : {dim: indexer, }, optional
      Time attribute and values over which to subset the array. For example, use season='DJF' to select winter values,
      month=1 to select January, or month=[6,7,8] to select summer months. If not indexer is given, all values are
      considered.

    Returns
    -------
    xarray.DataArray
      An array of values with a 1/t probability of exceedance or non-exceedance when mode is high or low respectively.

    """
    # Apply rolling average
    attrs = da.attrs.copy()
    if window > 1:
        da = da.rolling(time=window).mean(allow_lazy=True, skipna=False)
        da.attrs.update(attrs)

    # Assign default resampling frequency if not provided
    freq = freq or default_freq(**indexer)

    # Extract the time series of min or max over the period
    sel = select_resample_op(da, op=mode, freq=freq, **indexer)

    # Frequency analysis
    return fa(sel, t, dist, mode)


def default_freq(**indexer):
    """Return the default frequency."""
    freq = "AS-JAN"
    if indexer:
        if "DJF" in indexer.values():
            freq = "AS-DEC"
        if "month" in indexer and sorted(indexer.values()) != indexer.values():
            raise NotImplementedError

    return freq


def get_dist(dist):
    """Return a distribution object from scipy.stats.
    """
    from scipy import stats

    dc = getattr(stats, dist, None)
    if dc is None:
        e = f"Statistical distribution `{dist}` is not found in scipy.stats."
        raise ValueError(e)
    return dc
