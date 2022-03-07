#!/bin/bash

# CHANGES REQUIRED TO SPACK PACKAGE FOR CHARMPP
#     https://github.com/spack/spack/issues/18535
# then...
# spack install namd @2.15a1 fftw=mkl ^charmpp backend=mpi arch=cascadelake
#
# ALSO, the NAMD package assumes that 'intel-mkl' sets up include directories like Spack expects
# It does not.
# Just yum install fftw-devel so that it can find an fftw3.h header...

# Reference: https://software.intel.com/content/www/us/en/develop/articles/recipe-build-and-run-namd-on-intel-xeon-processors-on-single-node.html

curl -O http://www.ks.uiuc.edu/Research/namd/utilities/apoa1.tar.gz
tar xfz apoa1.tar.gz
sed -i -e '/numsteps/s/500/1000/' apoa1/apoa1.namd
sed -i -e "/outputtiming/a\\outputenergies 600" apoa1/apoa1.namd

GET_PERF="\$2==\"Benchmark\"{n++; s+=log(\$8); }END{print 1/exp(s/n)}"

# Run 1 rank per host
mpirun -N 1 -np ${SLURM_JOB_NUM_NODES} namd2 +p ${SLURM_CPUS_ON_NODE} +ppn ${SLURM_CPUS_ON_NODE} +setcpuaffinity ./apoa1/apoa1.namd  > namd-apoa1.log 2>&1
res=$?

if [[ "$res" == 0 ]]; then
    kpi=$(awk "${GET_PERF}" < namd-apoa1.log)
    echo "{\"result_unit\": \"ns/day\", \"result_value\": $kpi}" > kpi.json
fi