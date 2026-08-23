/**
 * Example dataset configuration for each analysis type.
 *
 * Each entry maps form field names to public/ CSV paths and provides
 * sensible default parameters so the user can test-drive an analysis
 * without preparing their own data first.
 */

import { publicUrl } from "../api";

interface ExampleConfig {
  files: Record<string, string>;
  params: Record<string, string | number | boolean>;
}

const EXAMPLES: Record<string, ExampleConfig> = {
  deg: {
    files: {
      counts: "/examples/deg/raw_count.csv",
      metadata: "/examples/deg/metadata.csv",
    },
    params: {
      compare_field: "treatment",
      tested_levels: "salt",
      reference_level: "control",
      same_fields: "line,timepoint",
      padj_cutoff: 0.05,
      log2fc_cutoff: 1.0,
      min_total_count: 10,
      min_replicates: 2,
    },
  },

  gma: {
    files: {
      transcriptome: "/examples/gma/DEAT.csv",
      metabolome: "/examples/gma/ym_metab.csv",
      group: "/examples/gma/group.csv",
    },
    params: {},
  },

  dem: {
    files: {
      metabs: "/examples/dem/ym_metab.csv",
      metadata: "/examples/dem/metadata.csv",
    },
    params: {
      compare_field: "treatment",
      tested_levels: "salt",
      reference_level: "control",
      same_fields: "line,timepoint",
      padj_cutoff: 0.05,
      log2fc_cutoff: 1.0,
      vip_cutoff: 1.0,
      max_missing_fraction: 0.5,
      impute_method: "half-min",
      normalize: true,
      log_transform: true,
      min_replicates: 2,
      n_orthogonal_components: 1,
    },
  },
};

export async function loadExample(
  type: string,
  setFile: (name: string, file: File | null) => void,
  setParam: (name: string, value: string | number | boolean) => void,
  onLoading: (loading: boolean) => void,
): Promise<void> {
  const config = EXAMPLES[type];
  if (!config) return;

  onLoading(true);
  try {
    // Fetch all example files in parallel
    const entries = await Promise.all(
      Object.entries(config.files).map(async ([fieldName, url]) => {
        const resolvedUrl = publicUrl(url);
        const res = await fetch(resolvedUrl);
        if (!res.ok) throw new Error(`Failed to load example file: ${resolvedUrl} (${res.status})`);
        const blob = await res.blob();
        const filename = url.split("/").pop() || `${fieldName}.csv`;
        const file = new File([blob], filename, { type: "text/csv" });
        return [fieldName, file] as const;
      }),
    );

    // Fill files
    for (const [fieldName, file] of entries) {
      setFile(fieldName, file);
    }

    // Fill parameters
    for (const [key, value] of Object.entries(config.params)) {
      setParam(key, value);
    }
  } finally {
    onLoading(false);
  }
}
