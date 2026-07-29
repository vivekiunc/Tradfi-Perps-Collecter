import subprocess
import os
import shutil
from datetime import datetime

# --- CONFIGURATION ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def run_script(script_name, args=None):
    """Utility to run a python script and wait for it to finish."""
    script_path = os.path.join(BASE_DIR, script_name)
    cmd = ["python3", script_path]
    if args:
        cmd.extend(args)
    
    print(f"--- Starting {script_name} ---")
    result = subprocess.run(cmd, cwd=BASE_DIR)
    if result.returncode != 0:
        print(f"Error running {script_name}")
        return False
    return True

def main():
    # 1. Run the tradfi file (scrapes slugs)
    if not run_script("tradfi.py"): return

    # 2. Run the market pairs file (uses .cache folder)
    if not run_script("market_pairs.py"): return

    # 3. Run the pivot table file (creates the Excel matrix)
    if not run_script("pivot_by_exchange.py", ["cmc_market_pairs.csv", "--out", "tradfi2_matrix.xlsx"]): return

    # 4. Run the collect file
    if not run_script("collect.py"): return

    # --- ARCHIVE SECTION ---
    date_str = datetime.now().strftime("%Y-%m-%d")
    final_filename = f"TradFi_Report_{date_str}.xlsx"
    
    reports_dir = os.path.join(BASE_DIR, "Reports")
    if not os.path.exists(reports_dir):
        os.makedirs(reports_dir)

    current_excel_path = os.path.join(BASE_DIR, "tradfi2_matrix.xlsx")
    final_destination = os.path.join(reports_dir, final_filename)

    if os.path.exists(current_excel_path):
        shutil.copy2(current_excel_path, final_destination)
        print(f"--- SUCCESS ---")
        print(f"Daily archive saved to: {final_destination}")
    else:
        print(f"Error: Could not find tradfi2_matrix.xlsx to archive.")

    # --- CLEANUP SECTION (The "rm -rf .cache" part) ---
    cache_folder = os.path.join(BASE_DIR, ".cache")
    if os.path.exists(cache_folder):
        print("Cleaning up .cache folder to ensure fresh data for next run...")
        # shutil.rmtree is the Python version of 'rm -rf'
        shutil.rmtree(cache_folder, ignore_errors=True)
        print("Cleanup complete.")

if __name__ == "__main__":
    main()