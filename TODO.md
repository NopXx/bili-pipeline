# TODO

- [ ] **ให้งาน HDR ใช้ Vulkan บน GPU ตัวเดียวกับที่งานได้รับ**
  งานแปลงสองงานพร้อมกันจะแยก GPU ด้วย `CUDA_VISIBLE_DEVICES` (`BILI_CONVERT_GPUS`) แต่เวลาเปิด GPU tonemap (`PREP_GPU_TONEMAP=1`)
  `public/prep-hls.sh` จะเรียก `-init_hw_device vulkan=vk` โดยไม่ระบุอุปกรณ์ Vulkan ไม่สน `CUDA_VISIBLE_DEVICES`
  งาน HDR สองงานจึงอาจไปทำ libplacebo tonemap บน GPU 0 ทั้งคู่
  - ส่ง index หรือชื่อ GPU ที่งานได้รับจาก `process_media.py` ให้ `prep-hls.sh` แล้วใช้ `vulkan=vk:<index>` ต้องเช็กว่าลำดับอุปกรณ์ Vulkan ตรงกับลำดับ CUDA
  - ทำทั้งเส้นทาง single-stream และ `gpu_hdr_ladder`
  - ยืนยันบน Kaggle (T4 x2) ด้วย `nvidia-smi` ระหว่างแปลง HDR สองงานพร้อมกัน

- [ ] **ทดสอบคิวแปลงสอง GPU กับงานจริงบน Kaggle** (commit `f1f93ce`)
  ส่งงานแปลง 3 งาน แล้วเช็กว่า 2 งานแรก `running` บน GPU 0/1 และงานที่ 3 รอคิว พร้อมดู `nvidia-smi` และ log ของ FFmpeg
  อย่ารีสตาร์ตเว็บบน Kaggle ระหว่างมีงานรันอยู่
