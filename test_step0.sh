#!/bin/sh

python3 -c "
from data_pipeline_manager import DataPipelineManager
mgr = DataPipelineManager(mode='local', gdrive_root='./gdrive')
#mgr.run_daily_pipeline(auto_universe=True)
mgr.run_weekly_pipeline()
"
