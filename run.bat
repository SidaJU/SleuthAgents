@echo off
REM ue-perf-sleuth 快捷启动
REM 用法: run.bat "为什么 xxx 一直发 query"
python -m perf_sleuth.cli %*
