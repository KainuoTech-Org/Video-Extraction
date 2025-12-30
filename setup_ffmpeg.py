import os
import urllib.request
import zipfile
import stat

def download_ffmpeg():
    url = "https://evermeet.cx/ffmpeg/ffmpeg-117621-g32616a440c.zip" # Fixed version for stability or use "https://evermeet.cx/ffmpeg/get/zip" for latest
    # 为了更稳定，使用一个较新的固定版本链接，或者直接用 latest
    url = "https://evermeet.cx/ffmpeg/get/zip"
    
    output_zip = "bin/ffmpeg.zip"
    output_bin = "bin/ffmpeg"
    
    if os.path.exists(output_bin):
        print("FFmpeg already exists.")
        return

    print(f"Downloading FFmpeg from {url}...")
    try:
        # 使用 User-Agent 避免被拦截
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(output_zip, 'wb') as out_file:
            out_file.write(response.read())
        print("Download complete.")
        
        print("Extracting...")
        with zipfile.ZipFile(output_zip, 'r') as zip_ref:
            zip_ref.extractall("bin")
            
        # 设置可执行权限
        st = os.stat(output_bin)
        os.chmod(output_bin, st.st_mode | stat.S_IEXEC)
        
        # 清理 zip
        os.remove(output_zip)
        print("FFmpeg setup successful!")
        
    except Exception as e:
        print(f"Failed to setup FFmpeg: {e}")

if __name__ == "__main__":
    download_ffmpeg()
