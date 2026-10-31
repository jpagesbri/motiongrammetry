# Shoe: GPU vs CPU in the Docker image (nvidia variant), 2026-10-31

The same image, default options, fresh setups, on `shoe.mp4` from Meta's uCO3D dataset (CC BY 4.0;
149 keyframes): RTX 3070 Ti vs Ryzen 7 5800X. Both pick the same mask model (IS-Net: BiRefNet needs
more than this 8 GB card).

| step    | GPU     | CPU      |
|---------|---------|----------|
| frames  | 0.1 min | 0.1 min  |
| mask    | 0.8 min | 2.2 min  |
| sfm     | 1.3 min | 2.1 min  |
| mvs     | 1.8 min | 9.2 min  |
| clean   | 1.3 min | 1.5 min  |
| texture | 1.1 min | 1.1 min  |
| total   | 6.4 min | 16.2 min |

Meshes, aligned on the camera centres, distances at the 100 mm print size:

| comparison                                  | median  | 90 %      | within 0.25 mm |
|---------------------------------------------|---------|-----------|----------------|
| CPU vs CPU (two runs: run-to-run variation) | 0.08 mm | 0.4 mm    | 83-84 %        |
| GPU vs CPU                                  | 0.33 mm | 1.4-1.8 mm| 40-41 %        |

The GPU's depth maps (OpenMVS CUDA) give a measurably different surface: bumpier on the plain pink
fabric, where there is little texture to match (comparison.jpg: shape rows and heatmap), while the
textured parts (laces, patterned heel, sole) agree. Textures look the same. The CPU run kept a stray
floor fragment (2 pieces), the GPU run did not (1 piece). Verdict: 2.5x faster, a little noisier on
plain surfaces (the editor's Smooth brush evens it out).

The first CPU run overlapped the test suite, so its timings are not used; its mesh is the second CPU
run of the run-to-run row.
