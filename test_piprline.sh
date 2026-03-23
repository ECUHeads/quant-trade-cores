#!/bin/sh
# ลบ LSTM model เก่า (binary) ที่ incompatible
rm -rf ./gdrive_test/models/*/lstm_*.pt
rm -rf ./gdrive_test/models/*/lstm_scaler_*.pkl

# ลบ sequence cache เก่า (อาจมี label format เก่า)
rm -rf ./gdrive_test/features/weekly/

# รัน weekly pipeline ใหม่
python3 data_pipeline_manager.py local
