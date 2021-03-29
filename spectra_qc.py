import argparse
import math
import os
import shutil
import sys
import time
import progressbar

import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from scipy.linalg import solve_banded
from scipy.signal import argrelmin, savgol_filter
from scipy.integrate import simpson


def parse_arguments():

    parser = argparse.ArgumentParser(
        description="Sort spectra by quality score using 4S baseline correction and Savitzky Golay based peak detection")

    parser.add_argument("DIR", help="Directory containing the spectra")
    parser.add_argument("-l", "--limits", metavar=("LOW", "HIGH"), type=float, nargs=2, default=[None, None],
                        help="Set limits to reduce the range of x-values")
    parser.add_argument("-p", "--penalty", type=int, default=0,
                        help="Penalty to the 2nd derivative used for smoothing; higher -> stronger smoothing")
    parser.add_argument("-b", "--buckets", type=int, default=400,
                        help="Number of buckets used for subsampling")
    parser.add_argument("-w", "--halfwidth", type=int, default=10,
                        help="Initial half width for the peak suppression algorithm, in number of buckets")
    parser.add_argument("-i", "--iterations", type=int, default=6,
                        help="Number of iterations for the peak suppression algorithm")
    parser.add_argument("-W", "--sgwindow", type=int, default=35,
                        help="Window width used for smoothing before detecting peaks.")
    parser.add_argument("-s", "--score", type=int, choices={0, 1, 2, 3, 4}, default=1,
                        help="Measure to use for scoring spectra; 0: None, use 1 as the base score; 1: Median peak height; 2: Mean peak height; 3: Mean peak area; 4: Total peak area")
    parser.add_argument("-n", "--npeaks", type=int, choices={0, 1, 2}, default=1,
                        help="How the number of peaks influences the score; 0: No influence; 1: Multiplicative, 2: Exponential")

    args = parser.parse_args()
    return args


def check_args_validity(args):
    if args.score == 0 and args.npeaks == 0:
        print("Error: Peak intensity and peak number cannot both be ignored")
        sys.exit()
    if args.sgwindow % 2 == 0:
        print("Error: Savitzky Golay window size must be odd")
        sys.exit()
    if args.penalty < 0:
        print("Error: Smoothing penalty must be positive or zero")
        sys.exit()
    if args.buckets <= 0:
        print("Error: Number of buckets must be positive")
        sys.exit()
    if args.halfwidth <= 0:
        print("Error: Suppression half width must be positive")
        sys.exit()
    if args.iterations <= 0:
        print("Error: Number of iterations must be positive")
        sys.exit()


def importFile(path, limit_low=None, limit_high=None):
    """Import the spectral data from a single file

    Args:
        path (str): Path to the file to be imported. 
        limit_low (int, optional): Lower limit for the spectral range. Defaults to None.
        limit_high (int, optional): Upper limit for the spectral range. Defaults to None.

    Returns:
        x (numpy.ndarray): x-values (e.g wavelengths, wavenumbers) of the imported spectrum.
        y (numpy.ndarray): y-values (e.g absorbance, transmission, intensity) of the imported spectrum.
    """

    spectrum = np.genfromtxt(path, delimiter=",")
    spectrum = np.transpose(spectrum)
    x = spectrum[0]
    y = spectrum[1]

    if limit_low is not None:
        try:
            limit_low_index = list(x).index(limit_low)
        except ValueError:
            print("Error: Lower limit out of range")
            sys.exit()
    else:
        limit_low_index = 0
        limit_low = x[0]

    if limit_high is not None:
        try:
            limit_high_index = list(x).index(limit_high)
        except ValueError:
            print("Error: Upper limit out of range")
            sys.exit()
    else:
        limit_high_index = len(x)
        limit_high = x[-1]

    x = x[limit_low_index:limit_high_index]
    y = y[limit_low_index:limit_high_index]

    bar1.update(bar1.value + 1)

    return x, y


def importDirectory(path, limit_low=None, limit_high=None):
    """Import the spectral data from all files in a given directory. 

    Args:
        path (str): Path to the directory from which spectra should be imported.
        limit_low (int, optional): Lower limit for the spectral range. Defaults to None.
        limit_high (int, optional): Upper limit for the spectral range. Defaults to None.

    Returns:
        x (numpy.ndarray): x-values (e.g wavelengths, wavenumbers) of the imported spectra. Each row represents one file.
        y (numpy.ndarray): y-values (e.g absorbance, transmission, intensity) of the imported spectra. Each row represents one file
        files (lst): List of imported files, ordered numerically
    """

    if not path.endswith("/"):
        path = path + "/"

    files = os.listdir(path)
    files = [file for file in files if file.lower().endswith(".txt")]

    files = sorted(files, key=lambda s: int(s[s.find("(")+1:s.find(")")]))

    x = []
    y = []

    for file in files:
        x0, y0 = importFile(path + file, limit_low, limit_high)
        x.append(x0)
        y.append(y0)
    return np.array(x), np.array(y), files


def _prep_buckets(buckets, len_x):
    """Calculate the position of the buckets for the subsampling step.

    Args:
        buckets (int or list/1D-array): Either the number of buckets, or a list of bucket positions.
        len_x (int): Number of data points per spectrum.

    Returns:
        lims (np.ndarray): Array of the bucket boundaries.
        mids (np.ndarray): Central position of every bucket.
    """
    if isinstance(buckets, int):
        lims = np.linspace(0, len_x-1, buckets+1, dtype=int)
    else:
        lims = buckets
        buckets = len(lims)-1

    mids = np.rint(np.convolve(lims, np.ones(2), 'valid') / 2).astype(int)
    mids[0] = 0
    mids[-1] = len_x - 1

    return lims, mids


def _prep_window(hwi, its):
    """Calculate the suppression window half-width for each iteration.

    Args:
        hwi (int): Initial half-width of the suppression window.
        its (int): Number of iterations for the main peak suppression loop.

    Returns:
        windows (np.ndarray): array of the exponentially decreasing window half-widths.
    """
    if its != 1:
        d1 = math.log10(hwi)
        d2 = 0

        tmp = np.array(range(its-1)) * (d2 - d1) / (its - 1) + d1
        tmp = np.append(tmp, d2)
        windows = np.ceil(10**tmp).astype(int)
    else:
        windows = np.array((hwi))
    return windows


def smooth_whittaker(y, pen):
    """Smooth data with a Whittaker Smoother.

    Args:
        y (np.ndarray): The data to be smoothed, with each row representing one dataset (spectrum, etc).
        dd (np.ndarray): Smoothing matrix for the whittaker algorithm.

    Returns:
        y_smooth (np.ndarray): The smoothed data.
    """
    diag = np.zeros((5, 5))
    np.fill_diagonal(diag, 1)
    middle = np.matmul(np.diff(diag, n=2, axis=0).T,
                       np.diff(diag, n=2, axis=0))
    zeros = np.zeros((2, 5))

    to_band = np.vstack((zeros, middle, zeros))
    the_band = np.diag(to_band)

    for i in range(1, 5):
        the_band = np.vstack((the_band, np.diag(to_band, -i)))

    indices = [0, 1] + [2] * (np.shape(y)[1]-4) + [3, 4]
    sparse_matrix = the_band[:, indices] * (10 ** pen)
    sparse_matrix[2, ] = sparse_matrix[2, ] + 1

    y_smooth = solve_banded((2, 2), sparse_matrix, y.T).T
    return y_smooth


def subsample(y, lims):
    """Split the data into equally sized buckets and return the minimum of each bucket. 

    Args:
        y (np.ndarray): The data to be subsampled.
        lims (np.ndarray): The boundaries of the buckets.
        buckets ([type]): The number of buckets.

    Returns:
        y_subs (np.ndarray): The minimum value for each bucket. 
    """
    buckets = len(lims) - 1
    y_subs = np.zeros(buckets)
    for i in range(buckets):
        y_subs[i] = np.min(y[lims[i]:lims[i+1]])

    return y_subs


def suppression(y_subs, buckets, its, windows):
    """Suppress peaks in the data, once each going forward and backward through the data. 

    Args:
        y_subs (np.ndarray): The subsampled data
        buckets (int): The number of buckets used for subsampling
        its (int): The number of iterations for peak suppression
        windows (np.ndarray): The exponentially decreasing window widths for each iteration.

    Returns:
        y_subs (np.ndarray): The subsampled data with peaks suppressed
    """

    for i in range(its):
        w0 = windows[i]

        for j in range(1, buckets):
            v = min(j, w0, buckets-j)
            a = np.mean(y_subs[j-v:j+v+1])
            y_subs[j] = min(a, y_subs[j])

        for j in range(buckets-1, 0, -1):
            v = min(j, w0, buckets-j)
            a = np.mean(y_subs[j-v:j+v+1])
            y_subs[j] = min(a, y_subs[j])

    return y_subs


def peakFill_4S(y, pen, hwi, its, buckets):
    """Baseline correction as outlined in Liland, K. H. (2015) ‘4S Peak Filling - Baseline estimation by iterative mean suppression’, 
    MethodsX. Elsevier, 2, pp. 135–140. doi: 10.1016/j.mex.2015.02.009.

    Args:
        y (np.ndarray): Array of y values of the spectra for which the baseline should be estimated. Each row should represent one spectrum
        pen (int): Penalty to the 2nd derivative for the smoothing algorithm. See "_prep_smoothing_matrix"
        hwi (int): Initial half-width (in buckets) of the suppression algorithm
        its (int): Number of iterations of the suppression algorithm
        buckets (int or list/1D-array): Either the number of buckets, or a list of bucket positions.

    Returns:
        y_corrected (np.ndarray): Baseline corrected y values
    """

    dims = np.shape(y)
    baseline = np.zeros(dims)

    lims, mids = _prep_buckets(buckets, dims[1])
    windows = _prep_window(hwi, its)

    y_smooth = smooth_whittaker(y, pen)

    for s in range(len(y)):
        y_subs = subsample(y_smooth[s], lims)
        y_supr = suppression(y_subs, buckets, its, windows)
        baseline[s] = np.interp(range(dims[1]), mids, y_supr)
        bar2.update(bar2.value + 1)

    y_corrected = y - baseline
    return y_corrected


def peakRecognition(x, y, sg_window):
    """[summary]

    Args:
        y ([type]): [description]
        sg_window ([type]): [description]

    Returns:
        [type]: [description]
    """

    corrected_sg2 = savgol_filter(
        y, window_length=sg_window, polyorder=3, deriv=2)

    peaks_all = []

    for i, row in enumerate(corrected_sg2):
        threshold = 0.05 + np.max(y[i])/30000
        peaks = argrelmin(row)[0]
        peaks = [peak for peak in peaks if row[peak] < -threshold]

        peak_condensing = []
        peaks_condensed = []
        for j in range(len(row)):
            if j in peaks:
                peak_condensing.append(j)
            if row[j] > 0 and len(peak_condensing) > 0:
                peaks_condensed.append(int(np.mean(peak_condensing)))
                peak_condensing = []
        if len(peak_condensing) > 0:
            peaks_condensed.append(int(np.mean(peak_condensing)))

        peaks_all.append(peaks_condensed)
        bar3.update(bar3.value + 1)

    return peaks_all


def calc_scores(x, y, peaks, score_measure, n_peaks_influence):
    """[summary]

    Args:
        x ([type]): [description]
        y ([type]): [description]
        peaks ([type]): [description]
        score_measure ([type]): [description]
        n_peaks_influence ([type]): [description]

    Returns:
        [type]: [description]
    """

    scores = []
    n_peaks_all = []

    for i, row in enumerate(peaks):
        n_peaks = len(row)
        if score_measure == 0:
            score = 1
        elif score_measure == 1:  # median height
            heights = [y[i, k] for k in row]
            score = np.median(heights)
        elif score_measure == 2:  # mean height
            heights = [y[i, k] for k in row]
            score = np.mean(heights)
        elif score_measure == 3:  # mean area
            score = simpson(y[i], x[i]) / n_peaks
        elif score_measure == 4:  # mean area
            score = simpson(y[i], x[i])

        scores.append(score)
        n_peaks_all.append(n_peaks)

        if n_peaks_influence == 0:
            scores_peaks = scores
        elif n_peaks_influence == 1:
            scores_peaks = [n*score for n, score in zip(n_peaks_all, scores)]
        elif n_peaks_influence == 2:
            scores_peaks = [score**(n/np.mean(n_peaks_all))
                            for n, score in zip(n_peaks_all, scores)]

        bar4.update(bar4.value + 1)

    return scores_peaks, scores, n_peaks_all


def export_sorted(path, files, scores, x, y_corr):
    """[summary]

    Args:
        path ([type]): [description]
        files ([type]): [description]
        scores ([type]): [description]
        x ([type]): [description]
        y_corr ([type]): [description]
    """

    dest_raw = os.path.join(path, "sorted_spectra")
    dest_corr = os.path.join(path, "baseline_corrected")

    if not os.path.exists(dest_raw):
        os.mkdir(dest_raw)
    if not os.path.exists(dest_corr):
        os.mkdir(dest_corr)

    files_sorted = [item for item in sorted(zip(scores, files))]
    files_sorted.reverse()

    for i in range(len(files_sorted)):
        file = files_sorted[i][1]
        src_file = os.path.join(path, file)
        dest_raw_file = os.path.join(dest_raw, file)
        new_file = str(i+1) + "_" + file
        new_file_raw = os.path.join(dest_raw, new_file)
        i_orig = files.index(file)

        if os.path.exists(new_file_raw):
            if os.path.samefile(src_file, new_file_raw):
                continue
            os.remove(new_file_raw)

        shutil.copy(src_file, dest_raw)
        os.rename(dest_raw_file, new_file_raw)

        dest_corr_file = os.path.join(dest_corr, new_file)
        with open(dest_corr_file, "w+") as f:
            for j in range(len(x[i_orig])):
                f.write(str(x[i_orig, j]) + "," + str(y_corr[i_orig, j]))

        bar5.update(bar5.value + 1)

    return files_sorted


if __name__ == '__main__':

    args = parse_arguments()

    start_time = time.perf_counter()

    n_files = len([file for file in os.listdir(args.DIR)
                   if file.lower().endswith(".txt")])

    bar1 = progressbar.ProgressBar(
        prefix='Importing Files.......',
        max_value=n_files)

    x, y, files = importDirectory(args.DIR, args.limits[0], args.limits[1])

    bar1.finish()
    bar2 = progressbar.ProgressBar(
        prefix='Estimating Baseline...',
        max_value=n_files)

    y_corrected = peakFill_4S(
        y, args.penalty, args.halfwidth, args.iterations, args.buckets)

    bar2.finish()
    bar3 = progressbar.ProgressBar(
        prefix='Detecting Peaks.......',
        max_value=n_files)

    peaks = peakRecognition(x, y_corrected, args.sgwindow)

    bar3.finish()
    bar4 = progressbar.ProgressBar(
        prefix='Calculating Scores....',
        max_value=n_files)

    scores_peaks, scores, n_peaks = calc_scores(
        x, y_corrected, peaks, args.score, args.npeaks)

    bar4.finish()
    bar5 = progressbar.ProgressBar(
        prefix='Exporting Data........',
        max_value=n_files)

    files_sorted = export_sorted(args.DIR, files, scores_peaks, x, y_corrected)

    bar5.finish()

    row_format = "{:<25} {:<25}"
    print("="*50)
    print(row_format.format("File", "Score"))
    print("_"*50)
    for score, file in files_sorted:
        print(row_format.format(file, int(score)))
    print("="*50)

    end_time = time.perf_counter()

    print(
        f"Analyzed {len(files)} files in {round(end_time-start_time, 2)} seconds.")

    sns.set(style="ticks")

    fig, ((ax_box1, ax_box2), (ax_hist1, ax_hist2)) = plt.subplots(
        2, 2, sharex="col", gridspec_kw={"height_ratios": (.15, .85)})

    sns.boxplot(x=n_peaks, ax=ax_box1)
    sns.boxplot(x=scores, ax=ax_box2)
    sns.histplot(n_peaks, ax=ax_hist1)
    sns.histplot(scores, ax=ax_hist2)

    ax_box1.set(yticks=[])
    ax_box2.set(yticks=[])
    sns.despine(ax=ax_hist1)
    sns.despine(ax=ax_hist2)
    sns.despine(ax=ax_box1, left=True)
    sns.despine(ax=ax_box2, left=True)

    score_names = {0: "No Score",
                   1: "Median Height",
                   2: "Mean Height",
                   3: "Mean Area",
                   4: "Total Area"}

    ax_hist1.set_xlabel("Number of Peaks")
    ax_hist2.set_xlabel(score_names[args.score])

    ax_box1.tick_params(axis="x", labelbottom=True)
    ax_box2.tick_params(axis="x", labelbottom=True)

    plt.show()
