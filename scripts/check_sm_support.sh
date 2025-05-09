#!/bin/bash

# Script to check supported CUDA SM architectures (PTX and SASS)
# for libraries (.so, .a) and executables in a given directory or for a single file.

# --- Global variables ---
cuobjdump_path=""
declare -a cuda_files_array # Global array to store files that might contain CUDA code

# --- Core terminal display functions ---

# Clear the current terminal line
clear_line() {
    # Use ANSI escape sequence to clear the entire line and return to beginning
    printf "\033[2K\r"
}

# Clear line and print a message
clear_and_print() {
    clear_line
    echo "$@"
}

# Clear line and print error message to stderr
print_error() {
    echo -e "\nERROR: $*" >&2
}

# Clear line and print progress indicator
print_progress() {
    local current="$1"
    local total="$2"
    local filename="$3"

    clear_line
    printf "[%d/%d] Processing %s" "$current" "$total" "$filename"
}

# --- Function to extract SM architecture versions ---
get_sm_versions() {
    local file="$1"
    local mode="$2"  # -lptx or -lelf

    output=$("$cuobjdump_path" "$mode" "$file" 2>&1)
    if [ $? -ne 0 ] && echo "$output" | grep -qv -e '^$' -e '^member ' -e 'does not contain device code'; then
        print_error "$output"
        return 1
    fi

    # Extract SM numbers
    local sm_in_pattern='.*(sm_[0-9]*[a-z]?).*'
    local sm_out_pattern='\1'
    # local sm_in_pattern='.*sm_([0-9]+)([0-9][af]?).*'
    # local sm_out_pattern='\1.\2'
    local count_in_pattern='^( *)([0-9]+)'
    local count_out_pattern='\1[\2]'
    echo "$output" | \
        sed -En "s|$sm_in_pattern|$sm_out_pattern|p" | \
        sort -V | \
        uniq -c | \
        sed -E "s|$count_in_pattern|$count_out_pattern|"
}

# --- Function to process a single file ---
process_file() {
    local file="$1"

    # Get PTX and SASS versions using helper function
    local ptx_versions=$(get_sm_versions "$file" "-lptx")
    local elf_versions=$(get_sm_versions "$file" "-lelf")

    # Check if any CUDA info was found
    if [[ -z "$ptx_versions" && -z "$elf_versions" ]]; then
        return 1 # Indicate no CUDA info found
    fi

    # Print results with clear headers and indentation
    clear_and_print "File: $file"
    if [[ -n "$elf_versions" ]]; then
        printf "%s\\n" "$elf_versions"
    fi
    if [[ -n "$ptx_versions" ]]; then
        printf "%s\\n" "$ptx_versions" | sed 's/$/ (PTX)/'
    fi
    echo ""

    return 0 # Indicate success (found CUDA info)
}

# --- Function to find files that may contain CUDA code ---
# Populates the global cuda_files_array
#
#  First find all candidate files (.so, .a, and executables)
#  Then filter them by MIME type to exclude scripts
#
find_files_to_check() {
    local dir="$1"

    # Check if directory exists first
    if [ ! -d "$dir" ]; then
        print_error "Directory '$dir' does not exist."
        return 1
    fi

    # Clear the global array
    cuda_files_array=()

    # Search for files
    while IFS= read -r -d $'\0' file; do
        # Filter out shell and python scripts by checking MIME type
        local mime_type
        mime_type=$(file --mime-type -b "$file" 2>/dev/null)
        if [[ "$mime_type" != "text/x-shellscript" && "$mime_type" != "text/x-python" ]]; then
            cuda_files_array+=("$file")
        fi
    # Safety: Using find -print0 with null byte separators (\0) and process substitution to
    # correctly handle filenames containing spaces, newlines, or other special characters.
    # Standard word splitting with unquoted variables would break filenames apart at spaces.
    done < <(find "$dir" -type f \( -name "*.so" -o -name "*.a" -o -executable \) -print0)

    # No files found is not an error condition, just return success with empty array
    return 0
}

# --- Function to process an entire directory ---
process_dir() {
    local dir="$1"

    # Check if directory exists first
    if [ ! -d "$dir" ]; then
        print_error "Directory '$dir' does not exist."
        return 1
    fi

    # Get all files from the directory that might contain CUDA code
    if ! find_files_to_check "$dir"; then
        return 1
    fi

    # Get total number of files to process
    local total_files=${#cuda_files_array[@]}

    # Check if any files were found
    if [ $total_files -eq 0 ]; then
        clear_and_print "No relevant files (.so, .a, executables) found in this directory."
        return 0
    fi

    # Track file number for progress reporting
    local current_file_id=0

    # Track if we found any valid files with CUDA code
    local found_cuda=false

    # Process each file with proper quoting to handle spaces in filenames
    for file in "${cuda_files_array[@]}"; do
        ((current_file_id++))

        print_progress "$current_file_id" "$total_files" "$file"

        if process_file "$file"; then
            found_cuda=true
        fi
    done

    # Clear the progress line at the end (if the last file didn't have output)
    clear_line

    # If no files had CUDA code, inform the user
    if [ "$found_cuda" = false ]; then
        clear_and_print "No CUDA SM architectures found in any files in this directory."
    fi

    return 0
}

# --- Function to find CUDA toolkit binaries ---
find_cuda_tool() {
    local tool_name="$1"
    local default_path="/usr/local/cuda/bin/$tool_name"
    local found_path=""

    if [ -x "$default_path" ]; then
        found_path="$default_path"
    else
        echo "WARNING: $tool_name not found at default location '$default_path'. Searching PATH..."
        local path_in_env
        path_in_env=$(which "$tool_name" 2>/dev/null)
        if [ -n "$path_in_env" ] && [ -x "$path_in_env" ]; then
            found_path="$path_in_env"
            echo "INFO: Found $tool_name at: $found_path"
        fi
    fi

    if [ -z "$found_path" ]; then
        print_error "$tool_name not found. Please install CUDA toolkit or add it to your PATH."
        return 1
    fi

    # Check if tool can actually run
    if ! "$found_path" --version >/dev/null 2>&1; then
        print_error "$tool_name found but failed to execute"
        return 1
    fi

    # Set the corresponding global variable
    printf -v "${tool_name}_path" '%s' "$found_path"
    return 0
}

# --- Function to check for required dependencies ---
check_dependencies() {
    find_cuda_tool "cuobjdump" && find_cuda_tool "nvdisasm"
}

# --- Function to process the input ---
process_input() {
    # Parse args
    if [ -z "$1" ]; then
        echo "Usage: $0 <directory_or_file>"
        return 1
    fi
    local input_path="$1"

    # Invalid input check first
    if [ ! -d "$input_path" ] && [ ! -f "$input_path" ]; then
        print_error "Input path '$input_path' is not a valid directory or regular file."
        return 1
    fi

    clear_and_print "Checking CUDA SM support in '$input_path'..."
    clear_and_print "----------------------------------------------------------------------------------"

    if [ -d "$input_path" ]; then
        # Process as dir
        process_dir "$input_path"
    else
        # Process as file
        print_progress "1" "1" "$input_path"
        if ! process_file "$input_path"; then
            clear_and_print "No CUDA SM architectures found."
        fi
    fi

    clear_and_print "----------------------------------------------------------------------------------"
    clear_and_print "Check complete."

    return 0
}

# --- Main script logic ---
main() {
    # Check dependencies first
    if ! check_dependencies; then
        exit 1
    fi

    # Then process the input
    if ! process_input "$@"; then
        exit 1
    fi

    exit 0
}

# --- Script entry point ---
main "$@"
