"""Estimate periodic, nearly axial planes using NumPy; NiBabel reads NIfTI.

Install: python -m pip install numpy nibabel
Demo:    python detect_zebra_3d.py --demo
Image:   python detect_zebra_3d.py volume.nii.gz \
             --spacing-range 8 24 --max-tilt 25 --mask mask.nii.gz

Python API:
    volume, voxel_size = load_nifti("volume.nii.gz")
    result = detect_zebra_3d(volume, voxel_size, (8, 24), 25)
    # With a mask, use detect_zebra_nifti to also verify matching grids.

Axis 2 is the reference PLANE NORMAL, not a direction within the axial plane.
The NIfTI loader reorders/flips axes to RAS+: axis 0 left-to-right,
axis 1 posterior-to-anterior, axis 2 inferior-to-superior. No interpolation.
Only axis-aligned grids are supported by this loader; oblique/sheared affines
are rejected because the detector currently uses array-relative angles.
Voxel sizes follow ARRAY axis order.
Spacing is a complete bright-to-bright period, in voxel_size physical units.
Orientation is array-relative; no anatomical affine is inferred.

The output is a candidate estimate, not a calibrated detection decision.
Peak-to-shell-median power is NOT a p-value. Noise also has a maximum.
Harmonics can dominate; inspect the profile and verify estimates across ROIs.
FFT-bin estimates have finite resolution. Crop large images to limit memory.
No SciPy or plotting package is needed. NiBabel is only imported for NIfTI.
NIfTI spatial units are preserved, including unknown units: choose the spacing
range in those same units. Raw .npy arrays still require --voxel-size.
"""
import argparse
import json
import numpy as np


def _read_nifti(path):
    """Read a 3-D axis-aligned image, preserving units and applying scaling."""
    try:
        import nibabel as nib
    except ImportError as exc:
        raise ImportError("NIfTI input requires: python -m pip install nibabel") from exc
    image = nib.load(str(path))
    if len(image.shape) != 3:
        raise ValueError("NIfTI must be 3-D; select a volume explicitly for 4-D data")
    if int(image.header['sform_code']) == 0 and int(image.header['qform_code']) == 0:
        raise ValueError("NIfTI has no coded sform/qform; anatomical orientation is undefined")
    image = nib.as_closest_canonical(image)
    voxel_size = nib.affines.voxel_sizes(image.affine)
    directions = image.affine[:3, :3] / voxel_size[None, :]
    if not np.allclose(directions, np.eye(3), atol=1e-5, rtol=0):
        raise ValueError("Oblique/sheared NIfTI grid: canonical axis flips/permutations "
                         "do not align it with world axes. This loader needs an "
                         "axis-aligned grid, such as a standard MNI reference grid.")
    volume = image.get_fdata(dtype=np.float32, caching="unchanged")
    return volume, voxel_size, image.affine.copy(), image.header.get_xyzt_units()[0]


def load_nifti(path):
    """Return (volume, voxel_size) in exact RAS+ array orientation.

    Axes: 0 left-to-right, 1 posterior-to-anterior, 2 inferior-to-superior.
    Reorientation only flips/permutates voxels. Spatial units are those in the
    file header (usually mm); unknown units are not silently labelled as mm.
    Coded sform/qform required. Oblique/sheared or 4-D files are rejected.
    """
    volume, voxel_size, _, _ = _read_nifti(path)
    return volume, voxel_size


def detect_zebra_nifti(path, spacing_range, max_tilt_deg=25, mask_path=None):
    """Load a NIfTI image and optional binary mask, check grids, then estimate.

    spacing_range uses NIfTI spatial units. Output includes spatial_unit and
    the reoriented affine. Profile distances are relative, not MNI coordinates.
    """
    volume, vox, affine, unit = _read_nifti(path)
    mask = None
    if mask_path is not None:
        mask_values, _, mask_affine, mask_unit = _read_nifti(mask_path)
        if (mask_values.shape != volume.shape or mask_unit != unit
                or not np.allclose(mask_affine, affine, atol=1e-4, rtol=0)):
            raise ValueError("Mask and volume must share the same physical voxel grid and units")
        if not np.all(np.isfinite(mask_values) & ((mask_values == 0) | (mask_values == 1))):
            raise ValueError("NIfTI mask must contain only finite 0/1 values")
        mask = mask_values.astype(bool)
    result = detect_zebra_3d(volume, vox, spacing_range, max_tilt_deg, mask)
    result.update(voxel_size=vox, affine_ras=affine, spatial_unit=unit,
                  axis_directions=["left-to-right", "posterior-to-anterior", "inferior-to-superior"])
    return result


def _blur(array, sigma_voxel):
    """Separable, zero-padded Gaussian convolution, preserving array shape."""
    result = array
    for axis, sigma in enumerate(sigma_voxel):
        radius = int(np.ceil(3 * sigma))
        x = np.arange(-radius, radius + 1, dtype=float)
        kernel = np.exp(-0.5 * (x / sigma) ** 2)
        kernel /= kernel.sum()
        # Padding + valid convolution also works when kernel > image dimension.
        def convolve_line(line):
            return np.convolve(np.pad(line, (radius, radius)), kernel, mode="valid")
        result = np.apply_along_axis(convolve_line, axis, result)
    return result


def detect_zebra_3d(volume, voxel_size, spacing_range, max_tilt_deg=25, mask=None):
    """Return spacing, normal, tilt, spectral score and a plane-averaged profile.

    volume: real numeric 3-D array, each dimension >= 8.
    voxel_size: three positive finite sampling intervals in array-axis order.
    spacing_range: (min, max) period; min > twice the largest voxel dimension.
    max_tilt_deg: allowed normal angle from axis 2, in [0, 90).
    mask: optional Boolean array matching volume; nonfinite voxels are excluded.

    tilt_towards_axis0_deg and tilt_towards_axis1_deg are signed projected
    angles. normal_array_axes is a unit vector with positive axis-2 component.
    frequency_bin_width gives Fourier resolution in cycles per physical unit.
    profile_weight indicates support; weakly supported profile bins are NaN.
    """
    raw = np.asarray(volume)
    if (raw.ndim != 3 or min(raw.shape) < 8 or not np.isrealobj(raw)
            or not np.issubdtype(raw.dtype, np.number)):
        raise ValueError("volume must be a real numeric 3-D array with dimensions >= 8")
    vox = np.asarray(voxel_size, dtype=float)
    limits = np.asarray(spacing_range, dtype=float)
    if vox.shape != (3,) or not np.all(np.isfinite(vox) & (vox > 0)):
        raise ValueError("voxel_size must contain three positive finite values")
    if (limits.shape != (2,) or not np.all(np.isfinite(limits))
            or not 2 * vox.max() < limits[0] < limits[1]):
        raise ValueError("require 2*max(voxel_size) < min_spacing < max_spacing")
    if not np.isscalar(max_tilt_deg) or not np.isfinite(max_tilt_deg) or not 0 <= max_tilt_deg < 90:
        raise ValueError("max_tilt_deg must be in [0, 90)")
    if mask is None:
        valid = np.isfinite(raw)
    else:
        mask = np.asarray(mask)
        if mask.shape != raw.shape or mask.dtype != np.bool_:
            raise ValueError("mask must be Boolean and match volume.shape")
        valid = mask & np.isfinite(raw)
    if valid.sum() < 100:
        raise ValueError("fewer than 100 valid voxels")

    image = raw.astype(float, copy=True)
    centre = np.median(image[valid])
    image = np.where(valid, image - centre, 0.0)
    # Normalise before convolution/FFT to avoid unnecessary numerical scaling.
    magnitude = np.max(np.abs(image[valid]))
    if not np.isfinite(magnitude) or magnitude == 0:
        raise ValueError("volume has no usable intensity variation")
    image /= magnitude
    scale = 1.4826 * np.median(np.abs(image[valid]))
    if scale > 0:
        image = np.clip(image, -8 * scale, 8 * scale)
    sigma = (limits[1] / 2) / vox
    denominator = _blur(valid.astype(float), sigma)
    background = _blur(image, sigma) / np.maximum(denominator, np.finfo(float).eps)
    residual = np.where(valid, image - background, 0.0)
    weights = valid * _blur(valid.astype(float), (limits[0] / 4) / vox) ** 2
    for axis, length in enumerate(image.shape):
        shape = [1, 1, 1]
        shape[axis] = length
        weights *= np.hanning(length).reshape(shape)
    weight_sum = weights.sum()
    if weight_sum <= 0:
        raise ValueError("no usable tapered ROI")
    mean = np.sum(weights * residual) / weight_sum
    power = np.abs(np.fft.fftshift(np.fft.fftn((residual - mean) * weights))) ** 2
    if power.max() <= 0:
        raise ValueError("no nonzero spectral power")

    shape = np.array(image.shape)
    df = 1.0 / (shape * vox)
    axes = [np.fft.fftshift(np.fft.fftfreq(int(n), d)) for n, d in zip(shape, vox)]
    k0, k1, k2 = np.meshgrid(*axes, indexing="ij", sparse=True)
    radius = np.sqrt(k0*k0 + k1*k1 + k2*k2)
    band = (radius >= 1 / limits[1]) & (radius <= 1 / limits[0])
    candidates = band & (k2 > 0) & (k2 >= radius * np.cos(np.deg2rad(max_tilt_deg)))
    indices = np.flatnonzero(candidates)
    if not indices.size:
        raise ValueError("no FFT bins in search range; enlarge crop or search range")
    shell = np.floor(radius / df.max()).astype(np.int32)
    # All directions contribute to the radial baseline, not just the tilt cone.
    reference_shells = shell[band]
    reference_power = power[band]
    baseline = np.zeros(int(reference_shells.max()) + 1)
    for s in np.unique(reference_shells):
        baseline[s] = np.median(reference_power[reference_shells == s])
    floor_power = max(power.max() * 1e-12, np.finfo(float).tiny)
    scores = power.ravel()[indices] / np.maximum(baseline[shell.ravel()[indices]], floor_power)
    best = int(np.argmax(scores))
    peak = np.unravel_index(indices[best], image.shape)
    frequency = np.array([axes[d][peak[d]] for d in range(3)])
    normal = frequency / np.linalg.norm(frequency)

    del power, radius, shell, background, denominator
    coordinates = [np.arange(int(n)) * v * a for n, v, a in zip(shape, vox, normal)]
    position = (coordinates[0][:, None, None] + coordinates[1][None, :, None]
                + coordinates[2][None, None, :])
    use = valid & (weights > 0)
    t = position[use]
    origin = t.min()
    bin_width = vox.min() / 2
    bins = np.floor((t - origin) / bin_width).astype(np.intp)
    sums = np.bincount(bins, weights=weights[use])
    weighted = np.bincount(bins, weights=weights[use] * residual[use])
    profile = np.full(sums.shape, np.nan)
    np.divide(weighted, sums, out=profile, where=sums > 0)
    profile[sums < 0.05 * sums.max()] = np.nan
    profile *= magnitude
    return {
        "spacing": float(1 / np.linalg.norm(frequency)),
        "normal_array_axes": normal,
        "frequency_vector": frequency,
        "tilt_from_axis2_deg": float(np.rad2deg(np.arccos(np.clip(normal[2], -1, 1)))),
        "tilt_towards_axis0_deg": float(np.rad2deg(np.arctan2(normal[0], normal[2]))),
        "tilt_towards_axis1_deg": float(np.rad2deg(np.arctan2(normal[1], normal[2]))),
        "peak_to_shell_median": float(scores[best]),
        "frequency_bin_width": df,
        "profile_coordinate": origin + (np.arange(sums.size) + 0.5) * bin_width,
        "profile_mean": profile,
        "profile_weight": sums,
    }


def _demo():
    """Deterministic recovery checks; these do not calibrate false positives."""
    for period, tilt, vox, seed in [
        (16, 15, (2, 2, 2), 1),
        (14, -20, (1.5, 2, 2.5), 2),
        (12, 0, (2, 2, 2), 3),
    ]:
        rng = np.random.default_rng(seed)
        shape = (64, 72, 80)
        xyz = np.meshgrid(*[np.arange(n)*v for n, v in zip(shape, vox)], indexing="ij", sparse=True)
        normal = np.array([0, np.sin(np.deg2rad(tilt)), np.cos(np.deg2rad(tilt))])
        roi = sum(((x-x.mean())/(x.max()*0.46))**2 for x in xyz) < 1
        image = (3*np.cos(2*np.pi*sum(x*a for x, a in zip(xyz, normal))/period)
                 + rng.normal(size=shape) + 0.02*xyz[1])
        image.flat[rng.choice(image.size, 20, replace=False)] = 100
        result = detect_zebra_3d(image, vox, (8, 24), 30, roi)
        spacing_error = abs(result["spacing"]-period)/period
        angle_error = np.rad2deg(np.arccos(np.clip(np.dot(normal, result["normal_array_axes"]), -1, 1)))
        print(f'True spacing {period:g}, tilt {tilt:g} deg -> '
              f'estimated {result["spacing"]:.3f}, {result["tilt_towards_axis1_deg"]:.3f} deg; '
              f'spacing error {spacing_error:.1%}, normal error {angle_error:.2f} deg')
        assert spacing_error < 0.08 and angle_error < 5, "synthetic recovery failed"
    try:
        detect_zebra_3d(np.ones((16, 16, 16)), (1, 1, 1), (4, 8))
    except ValueError:
        pass
    else:
        raise AssertionError("constant volume should be rejected")
    print("All demo checks passed. Scores are not calibrated detection thresholds.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("volume", nargs="?", help="3-D .nii, .nii.gz or .npy file")
    parser.add_argument("--voxel-size", type=float, nargs=3, help="required only for .npy input")
    parser.add_argument("--spacing-range", type=float, nargs=2)
    parser.add_argument("--max-tilt", type=float, default=25)
    parser.add_argument("--mask", help="binary NIfTI mask, or Boolean .npy mask for .npy input")
    parser.add_argument("--output", help="optional .npz path for estimates and profile")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    if args.demo:
        _demo()
        return
    if args.volume is None or args.spacing_range is None:
        parser.error("provide volume and --spacing-range, or use --demo")
    if args.volume.lower().endswith(".npy"):
        if args.voxel_size is None:
            parser.error(".npy input requires --voxel-size")
        result = detect_zebra_3d(np.load(args.volume, allow_pickle=False), args.voxel_size,
                             args.spacing_range, args.max_tilt,
                             None if args.mask is None else np.load(args.mask, allow_pickle=False))
    else:
        if args.voxel_size is not None:
            parser.error("NIfTI voxel sizes come from its affine; omit --voxel-size")
        result = detect_zebra_nifti(args.volume, args.spacing_range, args.max_tilt, args.mask)
    summary = {k: (v.tolist() if isinstance(v, np.ndarray) else v)
               for k, v in result.items() if not k.startswith("profile_")}
    print(json.dumps(summary, indent=2))
    if args.output:
        np.savez_compressed(args.output, **result)


if __name__ == "__main__":
    main()
