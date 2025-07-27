import struct
import re
import os
import argparse
import math
import json
from datetime import datetime

def find_files(directory):
    """在指定目录中自动查找所需的数据文件。"""
    print(f"正在目录 '{directory}' 中搜索文件...")
    files = {
        "d_txt": None,
        "base_txt": None,
        "info_json": None
    }
    for f in os.listdir(directory):
        if f.lower().startswith("eis_") and f.lower().endswith("_d.txt"):
            files["d_txt"] = os.path.join(directory, f)
        elif f.lower().startswith("eis_") and f.lower().endswith("_base.txt"):
            files["base_txt"] = os.path.join(directory, f)
        elif f.lower() == "info.json":
            files["info_json"] = os.path.join(directory, f)
            
    if not all(files.values()):
        missing = [k for k, v in files.items() if v is None]
        raise FileNotFoundError(f"错误: 在目录中找不到以下必须的文件: {', '.join(missing)}")
        
    print("成功找到所有必需的文件！")
    return files["d_txt"], files["base_txt"], files["info_json"]

def parse_d_txt_comprehensive(d_txt_path):
    """从 _D.txt 文件中解析所有高频陀螺仪样本和元数据。"""
    print(f"正在全面解析陀螺仪文件: {os.path.basename(d_txt_path)}")
    gyro_data = []
    metadata = {}
    
    gyro_regex = re.compile(r'TimeStamp:(\d+),value:([^,]+),SensorType:4')
    frame_regex = re.compile(r'FrameIndex:\d+,TimeStamp:(\d+)')
    shutter_skew_regex = re.compile(r'ShutterSkew:(\d+)')
    focal_length_regex = re.compile(r'FocalLength:([\d\.]+)')

    with open(d_txt_path, 'r', encoding='utf-8') as f:
        for line in f:
            gyro_match = gyro_regex.search(line)
            if gyro_match:
                timestamp_ns = int(gyro_match.group(1))
                values = gyro_match.group(2).split(';')
                gx, gy, gz = map(float, values[0:3])
                gyro_data.append({'timestamp_ns': timestamp_ns, 'gx': gx, 'gy': gy, 'gz': gz})
            
            frame_match = frame_regex.search(line)
            if frame_match:
                if "shutter_skew" not in metadata:
                    skew_match = shutter_skew_regex.search(line)
                    if skew_match:
                        metadata["shutter_skew"] = int(skew_match.group(1))
                if "focal_length" not in metadata:
                    fl_match = focal_length_regex.search(line)
                    if fl_match:
                         metadata["focal_length"] = float(fl_match.group(1))

    if not gyro_data:
        raise ValueError("_D.txt 文件中未找到 SensorType:4 的陀螺仪数据。")
    
    gyro_data.sort(key=lambda x: x['timestamp_ns'])

    first_ts = gyro_data[0]['timestamp_ns']
    last_ts = gyro_data[-1]['timestamp_ns']
    duration_s = (last_ts - first_ts) / 1_000_000_000.0
    sample_rate_hz = len(gyro_data) / duration_s if duration_s > 0 else 0

    print(f"成功从 .txt 文件中提取了 {len(gyro_data)} 个陀螺仪样本。")
    print(f"估算出的陀螺仪采样率约为: {sample_rate_hz:.2f} Hz")

    return gyro_data, metadata

def parse_additional_metadata(base_txt_path, info_json_path):
    """从 _BASE.txt 和 info.json 文件中解析其他元数据。"""
    metadata = {}
    with open(base_txt_path, 'r', encoding='utf-8') as f:
        content = f.read()
        phys_size = re.search(r'PhysicalSize:\[([\d\.]+)-([\d\.]+)\]', content)
        if phys_size:
            metadata['sensor_width'] = float(phys_size.group(1))
            metadata['sensor_height'] = float(phys_size.group(2))
        
        pix_array = re.search(r'PixelArraySize:\[(\d+)-(\d+)\]', content)
        if pix_array:
            metadata['image_width'] = int(pix_array.group(1))
            metadata['image_height'] = int(pix_array.group(2))

    with open(info_json_path, 'r', encoding='utf-8') as f:
        info = json.load(f)
        metadata['fps'] = info.get('fps', 29.97)
        metadata['calib_width'] = info.get('width', 2400)
        metadata['calib_height'] = info.get('height', 1344)

    return metadata

def write_gcsv(data, metadata, output_path, orientation):
    """将所有高频数据写入一个一体化的 .gcsv 文件。"""
    print(f"正在生成文件: {os.path.basename(output_path)}")

    first_timestamp_ns = data[0]['timestamp_ns'] if data else 0

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        f.write('GYROFLOW IMU LOG\r\n')
        f.write('version,1.3\r\n')
        f.write('id,CustomEISLogger\r\n')
        f.write(f'orientation,{orientation}\r\n')
        
        if metadata.get('shutter_skew'):
            readout_ms = metadata['shutter_skew'] / 1_000_000.0
            f.write(f'frame_readout_time,{readout_ms:.4f}\r\n')
        
        # gscale 是 1.0 因为陀螺仪数据已经是 rad/s
        # tscale 是 0.001 因为我们将提供的时间戳 't' 是毫秒
        f.write('gscale,1.0\r\n')
        f.write('tscale,0.001\r\n')
        
        # 写入数据表头
        f.write('t,gx,gy,gz\r\n')
        
        # 写入所有高频数据行
        for d in data:
            # 计算相对于第一个样本的时间（毫秒）
            time_ms = (d['timestamp_ns'] - first_timestamp_ns) / 1_000_000.0
            # .gcsv 格式使用原始的、未经旋转的数据
            line = f"{time_ms:.3f},{d['gx']:.6f},{d['gy']:.6f},{d['gz']:.6f}\r\n"
            f.write(line)
            
    print("文件写入成功！")

def write_final_lens_profile_json(metadata, output_path):
    """根据用户提供的精确校准数据和动态提取的元数据，生成最终的镜头配置文件。"""
    print(f"正在生成最终的镜头配置文件: {os.path.basename(output_path)}")
    
    # 使用用户提供的精确校准数据作为模板
    profile = {
      "name": "RayNeo_V3_IMX681__2k_16by9_2400x1344-29.97fps",
      "note": "Final profile combining calibrated data with dynamically extracted metadata.",
      "calibrated_by": "San Joeng",
      "camera_brand": "RayNeo",
      "camera_model": "V3",
      "lens_model": "IMX681",
      "camera_setting": "",
      "calib_dimension": {
        "w": metadata.get('calib_width', 2400),
        "h": metadata.get('calib_height', 1344)
      },
      "orig_dimension": {
        "w": metadata.get('image_width', 2400),
        "h": metadata.get('image_height', 1344)
      },
      "output_dimension": {
        "w": metadata.get('calib_width', 2400),
        "h": metadata.get('calib_height', 1344)
      },
      # 动态填充帧读取时间
      "frame_readout_time": metadata.get('shutter_skew', 0) / 1_000_000.0,
      "frame_readout_direction": "TopToBottom",
      "gyro_lpf": None,
      "input_horizontal_stretch": 1.0,
      "input_vertical_stretch": 1.0,
      "num_images": 15,
      # 动态填充帧率
      "fps": metadata.get('fps'),
      "crop": None,
      "official": False,
      "asymmetrical": False,
      "fisheye_params": {
        "RMS_error": 0.4294243819710179,
        # 硬编码用户提供的精确校准值
        "camera_matrix": [
          [1146.6817864492118, 0.0, 1198.184123927221],
          [0.0, 1143.0485308408684, 683.1106735346044],
          [0.0, 0.0, 1.0]
        ],
        "distortion_coeffs": [
          0.25042637237421467,
          0.561834527611872,
          -1.4730620460566122,
          1.835199944737525
        ],
        "radial_distortion_limit": None
      },
      "identifier": "",
      "calibrator_version": "1.6.1",
      "date": datetime.now().strftime("%Y-%m-%d"),
      "compatible_settings": [],
      "sync_settings": None,
      "distortion_model": None,
      "digital_lens": None,
      "digital_lens_params": None,
      "interpolations": None,
      "focal_length": metadata.get('focal_length'),
      "crop_factor": None,
      "global_shutter": False
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(profile, f, indent=2)
    print("最终版JSON文件写入成功！")

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="将专有的相机EIS数据转换为Gyroflow .gcsv和镜头配置.json格式。")
    parser.add_argument("directory", type=str, help="包含所有数据文件 (.txt, .json) 的文件夹路径。")
    # [v4.2] 移除 --orientation 参数，因为我们已经确定了正确的值
    args = parser.parse_args()

    # [v4.2] 硬编码已确认的正确轴向
    CORRECT_ORIENTATION = "YxZ"

    print(f"--- Gyroflow 终极数据转换脚本 (v4.2 - 单一配置: {CORRECT_ORIENTATION}) ---")
    
    try:
        d_txt_path, base_txt_path, info_json_path = find_files(args.directory)
        
        gyro_samples, metadata_from_d = parse_d_txt_comprehensive(d_txt_path)
        metadata_from_base_and_json = parse_additional_metadata(base_txt_path, info_json_path)
        
        metadata = {**metadata_from_d, **metadata_from_base_and_json}
        
        base_output_filename = os.path.splitext(os.path.basename(d_txt_path))[0].replace("_D", "")
        
        # [v4.2] 只生成一个使用正确轴向的 .gcsv 文件
        output_filename_gcsv = f"{base_output_filename}.gcsv"
        output_path_gcsv = os.path.join(args.directory, output_filename_gcsv)
        write_gcsv(gyro_samples, metadata, output_path_gcsv, CORRECT_ORIENTATION)
        
        # 生成一个最终的镜头配置文件
        output_filename_json = f"{base_output_filename}_final_lens_profile.json"
        output_path_json = os.path.join(args.directory, output_filename_json)
        write_final_lens_profile_json(metadata, output_path_json)
        
        print(f"\n✅ 成功！已在您的文件夹中生成最终的 .gcsv 和 .json 文件。")
        print("\n下一步操作:")
        print("1. 在Gyroflow中加载视频文件。")
        print("2. 加载生成的 `..._final_lens_profile.json` 文件。")
        print("3. Gyroflow应该会自动加载与视频同名的 .gcsv 文件。")

    except Exception as e:
        print(f"\n❌ 处理过程中发生严重错误: {e}")

if __name__ == '__main__':
    main()
