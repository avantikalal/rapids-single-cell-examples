# %%
from subprocess import Popen, PIPE

import os
import cudf
import torch
import time
import tabix
import numpy as np
import pandas as pd
from numba import cuda

import gzip

from collections.abc import Sequence

import torch
from atacworks.dl4atac.layers import ZeroSamePad1d, Activation, ConvAct1d, ResBlock
from atacworks.dl4atac.models.models import DenoisingResNet
from atacworks.dl4atac.models.model_utils import load_model


def count_fragments(fragment_file):
    """
    Count number of fragments per barcode in fragment file
    """
    fragment_barcodes=pd.read_csv(fragment_file, compression='gzip', sep='\t', header=None, usecols=[3])
    barcode_counts = fragment_barcodes.iloc[:,0].value_counts().reset_index()
    barcode_counts.columns = ['cell', 'fragments']
    return barcode_counts


def query_fragments(fragment_file, chrom, start, end):
    tb = tabix.open(fragment_file)
    results = tb.querys("%s:%d-%d" % (chrom, start, end))
    records = []
    for record in results:
        records.append(record)
    return records


def tabix_query(filename, chrom, start, end):
    """Call tabix and generate an array of strings for each line it returns."""
    query = '{}:{}-{}'.format(chrom, start, end)
    process = Popen(['tabix', '-f', filename, query], stdout=PIPE)
    records = []
    for line in process.stdout:
        record = line.decode('utf-8').strip().split('\t')
        records.append(record)
    return records


@cuda.jit
def expand_interval(start, end, index, end_index,
                    interval_start, interval_end, interval_index, step):
    for i in range(cuda.threadIdx.x, start.size, cuda.blockDim.x):
        # Starting position in the target frame
        first_index = end_index[i] - (end[i] - start[i])
        chrom_start = start[i]
        for j in range(first_index, end_index[i], step):
            interval_start[j] = chrom_start
            chrom_start += 1
            interval_end[j] = chrom_start
            interval_index[j] = index[i]


def read_fragments(chrom, start, end, fragment_file, pad=0):
    #Create a DF from the output of tabix query
    reads = cudf.DataFrame(
        data=tabix_query(fragment_file, chrom, start - pad, end + pad),
        columns=['chrom', 'start', 'end', 'cell', 'duplicate'])
    reads = reads.iloc[:, :4]
    reads['row_num'] = reads.index
    reads = reads.astype({"start": np.int32, "end": np.int32})
    reads['len'] = reads['end'] - reads['start']

    return reads


def get_coverages(reads_orig, chrom, start, end, pad=0):

    reads = reads_orig.copy()
    reads = reads.sort_values(by='row_num').reset_index(drop=True)
    
    # List all clusters
    clusters = sorted(np.unique(reads['cluster'].to_array()))
    num_clusters = len(clusters)

    # Get total window length
    window_size = end - start + 2*pad

    # Create expanded coverage dataframe
    expanded_coverage_size = reads['len'].sum()
    expanded_coverage = cudf.DataFrame()
    expanded_coverage['start'] = np.zeros(window_size, dtype=np.int32)
    expanded_coverage['end'] = np.zeros(window_size, dtype=np.int32)
    expanded_coverage['row_num'] = np.zeros(window_size, dtype=np.int32)
    expand_interval.forall(reads.shape[0])(
        reads['start'],
        reads['end'],
        reads['row_num'],
        reads['len'].cumsum(),
        expanded_coverage['start'],
        expanded_coverage['end'],
        expanded_coverage['row_num'],
        1)
    reads.drop(['start', 'end'], inplace=True)
    expanded_coverage = expanded_coverage.merge(reads, on='row_num')

    # Get summed coverage at each position
    summed_coverage = expanded_coverage.groupby(['chrom', 'start', 'end', 'cluster'], as_index=False).count()

    # Create empty array
    x = np.zeros(shape=(num_clusters, window_size))

    # Iterate over clusters to add coverage values
    for (i, cluster) in enumerate(clusters):
        df_group = summed_coverage.loc[summed_coverage['cluster'] == cluster]
        coords = df_group['start'] - start + pad
        values = df_group['row_num']
        ind = (coords >= 0) & (coords < (end-start+(2*pad)))
        coords = coords[ind].values.get()
        values = values[ind].values.get()
        x[i][coords] = values

    return x


def load_atacworks_model(weights_path, interval_size, gpu):
    model = DenoisingResNet(interval_size=interval_size, kernel_size=51, kernel_size_class=51)
    model = load_model(model, weights_path=weights_path, rank=0)
    model = model.cuda(gpu)
    return model


def reshape_with_padding(coverage, interval_size, pad):
    num_clusters = int(coverage.shape[0])
    padded_interval_size = int(interval_size + 2*pad)
    n_intervals = int((coverage.shape[1] - 2*pad) / interval_size)
    if n_intervals == 1:
        interval_starts = [0]
    else:
        interval_starts = range(0, coverage.shape[1] - padded_interval_size, interval_size + pad)
    reshaped_coverage = np.zeros(shape=(num_clusters*n_intervals, padded_interval_size))
    for i in range(num_clusters):
        reshaped_cluster_coverage = np.stack([coverage[i, start:start+padded_interval_size] for start in interval_starts])
        reshaped_coverage[i*n_intervals:(i+1)*n_intervals, :] = reshaped_cluster_coverage
    return reshaped_coverage


def atacworks_denoise(coverage, model, gpu, interval_size, pad):
    input_arr = reshape_with_padding(coverage, interval_size, pad)
    with torch.no_grad():
        input_arr = torch.tensor(input_arr, dtype=float)
        input_arr = input_arr.unsqueeze(1)
        input_arr = input_arr.cuda(gpu, non_blocking=True).float()
        pred = model(input_arr)
        pred = np.stack([x.cpu().numpy() for x in pred], axis=-1)
        center = range(pad, pred.shape[1] - pad)
        pred = pred[:, center, :]
        pred = pred.reshape((coverage.shape[0], coverage.shape[1] - 2*pad, pred.shape[2]))
        return pred

                