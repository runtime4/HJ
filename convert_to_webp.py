import os
import glob
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from PIL import Image

EPISODES_DIR = "episodes"

def convert_image(png_path):
    webp_path = png_path.rsplit(".", 1)[0] + ".webp"
    # If WEBP already exists, just delete the PNG
    if os.path.exists(webp_path):
        os.remove(png_path)
        return "skipped"
    
    try:
        img = Image.open(png_path)
        img.load()
        # Convert P/LA/PA to RGBA if necessary
        if img.mode in ("P", "LA", "PA") or ("transparency" in img.info):
            img = img.convert("RGBA")
        
        # WEBP doesn't support P, needs RGB or RGBA
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
            
        # Quality 92 is an excellent balance: virtually identical visually but ~1/10th the size
        img.save(webp_path, "WEBP", quality=92, method=4)
        img.close()
        
        # Remove the massive PNG file
        os.remove(png_path)
        return "converted"
    except Exception as e:
        return f"error: {e}"

def main():
    png_files = glob.glob(os.path.join(EPISODES_DIR, "*", "*.png"))
    total = len(png_files)
    print(f"Found {total} PNG files to convert to WEBP.")
    
    if total == 0:
        return

    converted = 0
    skipped = 0
    errors = 0
    
    start_time = time.time()
    
    # ProcessPoolExecutor for CPU-bound image compression
    with ProcessPoolExecutor() as executor:
        futures = [executor.submit(convert_image, f) for f in png_files]
        
        for count, future in enumerate(as_completed(futures), 1):
            res = future.result()
            if res == "converted":
                converted += 1
            elif res == "skipped":
                skipped += 1
            else:
                errors += 1
                
            if count % 100 == 0 or count == total:
                elapsed = time.time() - start_time
                rate = count / elapsed if elapsed > 0 else 0
                print(f"[{count}/{total}] Converted: {converted}, Skipped: {skipped}, Errors: {errors} ({rate:.1f} imgs/sec)")

    print(f"Done in {time.time() - start_time:.1f}s! Size reduced drastically.")

if __name__ == '__main__':
    main()
