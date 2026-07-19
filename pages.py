import os
import glob
import json

EPISODES_DIR = "episodes"

def generate_spa_viewer():
    if not os.path.exists(EPISODES_DIR):
        print(f"Error: '{EPISODES_DIR}' folder not found. Run your archiver first!")
        return

    # Find all numeric folders in the episodes directory
    episode_folders = [f for f in glob.glob(os.path.join(EPISODES_DIR, "*")) if os.path.isdir(f) and os.path.basename(f).isdigit()]
    
    # Sort them by integer value to maintain order
    episode_folders = sorted(episode_folders, key=lambda x: int(os.path.basename(x)))
    total_episodes = len(episode_folders)

    if total_episodes == 0:
        print(f"No episode folders found in '{EPISODES_DIR}'.")
        return

    print(f"Found {total_episodes} episodes. Generating Absolute Cinema files...")

    # 1. Build the Data Object mapping Episode -> Number of Panels
    ep_data = {}
    for folder in episode_folders:
        ep_num = int(os.path.basename(folder))
        panels = glob.glob(os.path.join(folder, "*.webp"))
        ep_data[ep_num] = len(panels)

    # 2. Write the data to a JS file so the browser can read it locally
    with open("data.js", "w", encoding="utf-8") as f:
        f.write(f"const comicData = {json.dumps(ep_data)};\n")
        f.write(f"const totalEpisodes = {total_episodes};\n")

    # 3. Create the minimalist index.html viewer (Absolute Cinema)
    viewer_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hand Jumper</title>
    <style>
        body, html { 
            margin: 0; 
            padding: 0; 
            background-color: #000; /* Pure black for cinema feel */
            display: flex; 
            flex-direction: column; 
            align-items: center; 
            width: 100%;
        }
        img { 
            width: 100%; 
            max-width: 100%; 
            height: auto; 
            display: block; 
            vertical-align: top; 
            margin: 0; 
            padding: 0; 
            border: none;
        }
        .comic-container { 
            display: flex; 
            flex-direction: column; 
            width: 100%; 
            line-height: 0;
            font-size: 0;
            gap: 0;
        }
        #loading { 
            color: #333; /* Very dim loading text so it doesn't distract */
            padding: 50px; 
            font-size: 16px; 
            font-family: sans-serif;
            text-align: center;
        }
        /* Hide scrollbar for the ultimate clean look (optional, webkit only) */
        ::-webkit-scrollbar {
            width: 0px;
            background: transparent;
        }
    </style>
    <script src="data.js"></script>
</head>
<body>
    <div class="comic-container" id="comic-container">
        <div id="loading">Initializing...</div>
    </div>

    <script>
        const urlParams = new URLSearchParams(window.location.search);
        let currentEp = parseInt(urlParams.get('ep')) || 1;
        document.title = `Hand Jumper | Ep. ${currentEp}`;

        if (!comicData[currentEp]) {
            document.getElementById('comic-container').innerHTML = `<div id="loading">Episode not found.</div>`;
        } else {
            document.getElementById('comic-container').innerHTML = ""; 
            const numPanels = comicData[currentEp];

            for (let i = 1; i <= numPanels; i++) {
                const panelNum = String(i).padStart(3, '0');
                const img = document.createElement('img');
                // Updated to match the new folder structure: episodes/{episode_no}/{panel_number_3_digits}.webp
                img.src = `episodes/${currentEp}/${panelNum}.webp`;
                img.loading = "lazy";
                document.getElementById('comic-container').appendChild(img);
            }
        }
    </script>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(viewer_content)

    # 4. Generate a minimalist Home Index
    index_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Hand Jumper Archive</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { background: #000; color: #555; font-family: sans-serif; text-align: center; padding: 40px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(60px, 1fr)); gap: 10px; max-width: 800px; margin: 40px auto; }
        a { background: #111; color: #aaa; padding: 10px; border-radius: 3px; text-decoration: none; transition: 0.3s; }
        a:hover { background: #fff; color: #000; }
        h1 { font-weight: normal; font-size: 24px; letter-spacing: 2px; }
    </style>
</head>
<body>
    <h1>SELECT EPISODE</h1>
    <div class="grid">\n"""

    for ep in sorted(ep_data.keys()):
        index_content += f'        <a href="index.html?ep={ep}">{ep}</a>\n'

    index_content += """    </div>
</body>
</html>"""

    with open("directory.html", "w", encoding="utf-8") as f:
        f.write(index_content)

    print("Done! Open 'directory.html' to enter Absolute Cinema.")

if __name__ == "__main__":
    generate_spa_viewer()
