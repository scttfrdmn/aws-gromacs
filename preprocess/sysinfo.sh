#!/usr/bin/env bash
# Hardware characterization for a benchmark instance: what CPU, and -- the number
# that actually explains the ns/day plateau -- ACHIEVED memory bandwidth (STREAM).
# Runs ON a spawned instance in the gromacs image (has gcc/build-essential).
# Writes sysinfo.json to S3 alongside the cell results.
#
# Honesty about virtualized EC2: nominal freq / topology / cache come from lscpu
# (reliable). DIMM speed is masked by the hypervisor (dmidecode -> Unknown), so
# we MEASURE bandwidth with STREAM instead of reading a spec. Sustained all-core
# AVX-512 clock under load usually needs MSR/turbostat (blocked off .metal), so
# live-freq is best-effort and may report nominal only -- flagged as such.
#
# Env: SYSINFO_S3  (s3 prefix for sysinfo.json), COMPLETION_FILE
set -euo pipefail
SYSINFO_S3="${SYSINFO_S3:?set SYSINFO_S3}"
COMPLETION_FILE="${COMPLETION_FILE:-/tmp/SPAWN_COMPLETE}"
WORK="${WORK:-/tmp/sysinfo}"; mkdir -p "$WORK"; cd "$WORK"
LOG="$WORK/sysinfo.log"; exec > >(tee -a "$LOG") 2>&1
trap 'aws s3 cp "$LOG" "$SYSINFO_S3/sysinfo.log" --only-show-errors 2>/dev/null || true' EXIT

echo "== lscpu =="
MODEL=$(lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -1)
SOCKETS=$(lscpu | sed -n 's/^Socket(s):[[:space:]]*//p')
CORES_PS=$(lscpu | sed -n 's/^Core(s) per socket:[[:space:]]*//p')
CPUS=$(lscpu | sed -n 's/^CPU(s):[[:space:]]*//p')
MHZ_MAX=$(lscpu | sed -n 's/^CPU max MHz:[[:space:]]*//p')
MHZ_CUR=$(lscpu | sed -n 's/^CPU MHz:[[:space:]]*//p')
L3=$(lscpu | sed -n 's/^L3 cache:[[:space:]]*//p')
NUMA=$(lscpu | sed -n 's/^NUMA node(s):[[:space:]]*//p')
FLAGS_AVX512=$(grep -om1 'avx512f' /proc/cpuinfo || echo "")

echo "== STREAM (achieved memory bandwidth) =="
# Canonical STREAM; big arrays so it exceeds cache (real DRAM bandwidth). OpenMP
# over all cores = aggregate achievable bandwidth, the ceiling MD runs into.
cat > stream.c <<'EOF'
#include <stdio.h>
#include <omp.h>
#include <sys/time.h>
#define N 80000000L
static double a[N], b[N], c[N];
double wt(){struct timeval t;gettimeofday(&t,0);return t.tv_sec+t.tv_usec*1e-6;}
int main(){
  #pragma omp parallel for
  for(long i=0;i<N;i++){a[i]=1.0;b[i]=2.0;c[i]=0.0;}
  double best=1e30;
  volatile double sink=0.0;   /* consume c[] so the triad can't be optimized away */
  double scale=3.0;
  for(int r=0;r<5;r++){
    scale += 1.0;             /* vary the op each rep -> loop cannot be hoisted */
    double t=wt();
    #pragma omp parallel for
    for(long i=0;i<N;i++) c[i]=a[i]+scale*b[i];   /* Triad */
    t=wt()-t; if(t<best)best=t;
    sink += c[r*7 % N];       /* force c[] to be materialized */
  }
  (void)sink;
  /* Triad moves 3 arrays * 8 bytes * N bytes; best time -> GB/s */
  printf("%.1f\n", 3.0*8.0*(double)N/best/1e9);
  return 0;
}
EOF
gcc -O3 -fopenmp -march=native stream.c -o stream 2>/dev/null || gcc -O3 -fopenmp stream.c -o stream
BW=$(OMP_NUM_THREADS=$CPUS OMP_PROC_BIND=spread ./stream)
echo "achieved triad bandwidth: ${BW} GB/s across ${CPUS} threads"

echo "== write sysinfo.json =="
cat > sysinfo.json <<EOF
{"model": "${MODEL}", "sockets": ${SOCKETS:-0}, "cores_per_socket": ${CORES_PS:-0},
 "cpus": ${CPUS:-0}, "mhz_max": ${MHZ_MAX:-0}, "mhz_nominal": ${MHZ_CUR:-0},
 "l3_cache": "${L3}", "numa_nodes": ${NUMA:-0},
 "avx512": $([ -n "$FLAGS_AVX512" ] && echo true || echo false),
 "stream_triad_gbps": ${BW:-0}}
EOF
cat sysinfo.json
aws s3 cp sysinfo.json "$SYSINFO_S3/sysinfo.json" --only-show-errors
touch "$COMPLETION_FILE"
echo "== done =="
