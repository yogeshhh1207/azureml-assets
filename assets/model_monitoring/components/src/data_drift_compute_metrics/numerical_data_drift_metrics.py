# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""This file contains the core logic for data drift compute metrics component for numerical columns."""
import numpy as np
import pandas as pd
import pyspark.sql as pyspark_sql
import pyspark.sql.functions as F
from scipy.stats import ks_2samp
from synapse.ml.exploratory import DistributionBalanceMeasure
from io_utils import get_output_spark_df, init_spark
from shared_utilities.histogram_utils import (
    get_dual_histogram_bin_edges,
    get_histograms,
)


def _map_production_df_columns_with_bins(
    production_df: pyspark_sql.DataFrame, production_df_count_freq_map: dict
):
    """Map numerical columns to calculated bins."""
    # UDF to map numerical column value to calculated bin_value
    def map_value_to_bucket(mapping_broadcasted, col_name):
        column_edges = mapping_broadcasted.value.get(col_name)

        def f(col_val):
            for column_edge in column_edges.items():
                val = float(col_val)
                if val < column_edge[1][1] and val >= column_edge[1][0]:
                    return column_edge[0]
                # handle last edge
                if val == column_edge[1][1]:
                    return column_edge[0]

        return F.udf(f)

    transformed_df = production_df
    mapping = production_df_count_freq_map["feature_bucket_edges_map"]
    spark = init_spark()
    broadcasted_prod_df_map = spark.sparkContext.broadcast(mapping)

    for column in production_df.columns:
        transformed_df = transformed_df.withColumn(
            column, map_value_to_bucket(broadcasted_prod_df_map, column)(F.col(column))
        )

    return transformed_df


def _calculate_column_value_frequency_bin_map_in_df(
    hist, numerical_columns, total_count
):
    """Calculate count per value, frequency of value per bin and map feature to histogram bin in a numerical column."""
    feature_bucket_count_map = {}
    feature_bucket_edges_map = {}
    feature_bucket_frequency_map = {}

    for col in numerical_columns:
        buckets = {}
        value_frequency = {}
        bucket_hist_map = {}
        col_hist = hist[col]
        for i in range(0, len(col_hist[0]) - 1):
            first_edge = col_hist[0][i]
            second_edge = col_hist[0][i + 1]
            bucket_name = str(round(first_edge, 2)) + "_" + str(round(second_edge, 2))
            val_in_range = col_hist[1][i]
            buckets[bucket_name] = val_in_range
            value_frequency[bucket_name] = buckets[bucket_name] / total_count
            bucket_hist_map[bucket_name] = [first_edge, second_edge]

        feature_bucket_count_map[col] = buckets
        feature_bucket_frequency_map[col] = value_frequency
        feature_bucket_edges_map[col] = bucket_hist_map

    return {
        "feature_bucket_count_map": feature_bucket_count_map,
        "feature_bucket_frequency_map": feature_bucket_frequency_map,
        "feature_bucket_edges_map": feature_bucket_edges_map,
    }


def _jensen_shannon_numerical(
    baseline_df: pyspark_sql.DataFrame,
    production_df: pyspark_sql.DataFrame,
    baseline_df_count,
    production_df_count,
    numerical_columns: list,
):
    """Calculate Jensen Shannon metric for numerical columns."""
    bin_edges = get_dual_histogram_bin_edges(
        baseline_df,
        production_df,
        baseline_df_count,
        production_df_count,
        numerical_columns,
    )
    baseline_histograms = get_histograms(baseline_df, bin_edges, numerical_columns)
    prod_histograms = get_histograms(production_df, bin_edges, numerical_columns)

    baseline_freq_count_map = _calculate_column_value_frequency_bin_map_in_df(
        baseline_histograms, numerical_columns, baseline_df_count
    )
    production_freq_count_map = _calculate_column_value_frequency_bin_map_in_df(
        prod_histograms, numerical_columns, production_df_count
    )

    reference_distribution = list(
        baseline_freq_count_map["feature_bucket_frequency_map"].values()
    )

    transformed_production_df = _map_production_df_columns_with_bins(
        production_df, production_freq_count_map
    )
    distributionBalanceMeasure = (
        DistributionBalanceMeasure()
        .setSensitiveCols(numerical_columns)
        .setReferenceDistribution(reference_distribution)
        .transform(transformed_production_df.select(numerical_columns))
    )

    output_df = distributionBalanceMeasure.select(
        "FeatureName", "DistributionBalanceMeasure.js_dist"
    )
    output_df = (
        output_df.withColumnRenamed("FeatureName", "feature_name")
        .withColumnRenamed("js_dist", "metric_value")
        .withColumn("data_type", F.lit("numerical"))
        .withColumn("metric_name", F.lit("JensenShannonDistance"))
    )

    return output_df


def _normalized_wasserstein(
    baseline_df: pyspark_sql.DataFrame,
    production_df: pyspark_sql.DataFrame,
    baseline_df_count: int,
    production_df_count: int,
    numerical_columns: list,
):
    """Calculate normalized Wassserstein distance for numerical columns."""
    bin_edges = get_dual_histogram_bin_edges(
        baseline_df,
        production_df,
        baseline_df_count,
        production_df_count,
        numerical_columns,
    )
    baseline_histograms = get_histograms(baseline_df, bin_edges, numerical_columns)
    prod_histograms = get_histograms(production_df, bin_edges, numerical_columns)

    baseline_freq_count_map = _calculate_column_value_frequency_bin_map_in_df(
        baseline_histograms, numerical_columns, baseline_df_count
    )
    production_freq_count_map = _calculate_column_value_frequency_bin_map_in_df(
        prod_histograms, numerical_columns, production_df_count
    )

    reference_distribution = list(
        baseline_freq_count_map["feature_bucket_frequency_map"].values()
    )
    transformed_production_df = _map_production_df_columns_with_bins(
        production_df, production_freq_count_map
    )

    distribution_balance_measures = (
        DistributionBalanceMeasure()
        .setSensitiveCols(numerical_columns)
        .setReferenceDistribution(reference_distribution)
        .transform(transformed_production_df.select(numerical_columns))
    )

    output_df = distribution_balance_measures.select(
        "FeatureName", "DistributionBalanceMeasure.wasserstein_dist"
    )

    output_df = (
        output_df.withColumnRenamed("FeatureName", "feature_name")
        .withColumnRenamed("wasserstein_dist", "metric_value")
        .withColumn("data_type", F.lit("Numerical"))
        .withColumn("metric_name", F.lit("NormalizedWassersteinDistance"))
    )

    # Normalize wasserstein distance
    baseline_df_stats = baseline_df.summary()
    baseline_df_stdev = (
        baseline_df_stats.where(F.col("summary") == "stddev").drop("summary").first()
    )
    baseline_df_stdev = baseline_df_stdev.asDict()

    def normalize_value(mapping):
        def f(wasserstein_dist_col, feature_name):
            std_dev = float(mapping.value.get(feature_name))
            norm = max(std_dev, 0.001)
            return wasserstein_dist_col / norm

        return F.udf(f)

    spark = init_spark()
    mapping = spark.sparkContext.broadcast(baseline_df_stdev)

    output_df = output_df.withColumn(
        "metric_value",
        normalize_value(mapping)(F.col("metric_value"), F.col("feature_name")),
    )
    return output_df


def _ks2sample_pandas_impl(
    baseline_df, production_df, baseline_count, production_count, numerical_columns
):
    """Test to determine data drift using Two-Sample Kolmogorov-Smirnov Test for numerical feature."""
    DATASET_SIZE = 10000
    if baseline_count < DATASET_SIZE and production_count < DATASET_SIZE:
        baseline_df = baseline_df.toPandas()
        production_df = production_df.toPandas()
        rows = []
        for column in numerical_columns:
            baseline_col = pd.Series(baseline_df[column])
            production_col = pd.Series(production_df[column])
            ks2s = ks_2samp(baseline_col, production_col).pvalue
            row = [column, float(ks2s), "numerical", "TwoSampleKolmogorovSmirnovTest"]
            rows.append(row)

        output_df = get_output_spark_df(rows)
        output_df.show(truncate=False)
        return output_df
    else:
        raise Exception(
            f"Cannot calculate two-sample Kolmogorov-Smirnov test on dataset with more than {DATASET_SIZE} entries"
        )


def _psi_numerical(
    baseline_df,
    production_df,
    baseline_df_count,
    production_df_count,
    numerical_columns,
):
    """PSI calculation for numerical columns."""
    bin_edges = get_dual_histogram_bin_edges(
        baseline_df,
        production_df,
        baseline_df_count,
        production_df_count,
        numerical_columns,
    )
    baseline_histograms = get_histograms(baseline_df, bin_edges, numerical_columns)
    prod_histograms = get_histograms(production_df, bin_edges, numerical_columns)

    rows = []
    for column in numerical_columns:
        # PSI can be infinity if bins have zero counts in them. Incrementing the count of each bin by 1
        baseline_hist_count = baseline_histograms[column][1]
        baseline_hist_count = [count + 1 for count in baseline_hist_count]

        production_hist_count = prod_histograms[column][1]
        production_hist_count = [count + 1 for count in production_hist_count]

        baseline_percent = [
            (count / baseline_df_count) for count in baseline_hist_count
        ]
        production_percent = [
            (count / production_df_count) for count in production_hist_count
        ]

        psi = 0.0
        for i in range(len(baseline_percent)):
            psi += (production_percent[i] - baseline_percent[i]) * np.log(
                production_percent[i] / baseline_percent[i]
            )

        row = [column, float(psi), "numerical", "PopulationStabilityIndex"]
        rows.append(row)

    output_df = get_output_spark_df(rows)
    return output_df


def compute_numerical_data_drift_measures_tests(
    baseline_df: pyspark_sql.DataFrame,
    production_df: pyspark_sql.DataFrame,
    baseline_df_count: int,
    production_df_count: int,
    numerical_metric: str,
    numerical_columns: list,
    numerical_threshold: float,
):
    """Compute Data drift metrics and tests for numerical columns."""
    if numerical_metric == "JensenShannonDistance":
        output_df = _jensen_shannon_numerical(
            baseline_df,
            production_df,
            baseline_df_count,
            production_df_count,
            numerical_columns,
        )
    elif numerical_metric == "NormalizedWassersteinDistance":
        output_df = _normalized_wasserstein(
            baseline_df,
            production_df,
            baseline_df_count,
            production_df_count,
            numerical_columns,
        )
    elif numerical_metric == "PopulationStabilityIndex":
        output_df = _psi_numerical(
            baseline_df,
            production_df,
            baseline_df_count,
            production_df_count,
            numerical_columns,
        )
    elif numerical_metric == "TwoSampleKolmogorovSmirnovTest":
        output_df = _ks2sample_pandas_impl(
            baseline_df, production_df, numerical_columns
        )
    else:
        raise Exception(f"Invalid metric {numerical_metric} for numerical feature")

    output_df = output_df.withColumn(
        "threshold_value", F.lit(numerical_threshold).cast("float")
    )
    return output_df
