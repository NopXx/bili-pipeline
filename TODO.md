# TODO

- [x] **HDR ใช้ GPU ตัวเดียวกับที่งานได้รับ** — ทดสอบบน Kaggle T4 x2 แล้ว (2026-09-27)
  Kaggle ไม่มี libplacebo/Vulkan จึงใช้ OpenCL `tonemap_opencl` แทน สองงานพร้อมกัน (`CUDA_VISIBLE_DEVICES=0` และ `1`)
  ใช้ GPU คนละตัวจริง ทั้ง NVDEC, OpenCL และ NVENC ต่างตัวยุ่งพอ ๆ กัน และจบพร้อมกัน
  ยังต้องระวัง: บนเครื่องที่มี libplacebo (Vulkan) Vulkan ไม่สน `CUDA_VISIBLE_DEVICES`

- [ ] **ความสว่างของ HDR→SDR แบบ OpenCL ต่างจากแบบ CPU**
  คลิปทดสอบ (testsrc ติดแท็ก PQ ไม่มี mastering metadata) ออกมามืดกว่า: YAVG ~82 เทียบกับ ~119 ของ CPU chain
  ทั้งที่ใช้ hable และ desat=0 เหมือนกัน ต้องเทียบกับไฟล์ HDR จริงด้วยตา ถ้ามืดไป ปรับ `peak`/`param` ของ `tonemap_opencl`

- [ ] **ทดสอบคิวแปลงสอง GPU ผ่านเว็บจริงบน Kaggle**
  ส่งงานแปลง 3 งานจากหน้าเว็บ แล้วเช็กว่า 2 งานแรก `running` บน GPU 0/1 และงานที่ 3 รอคิว (ระดับ engine ผ่านแล้ว)
  อย่ารีสตาร์ตเว็บบน Kaggle ระหว่างมีงานรันอยู่
