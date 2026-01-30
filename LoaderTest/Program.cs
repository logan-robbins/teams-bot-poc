using System;
using System.Runtime.InteropServices;

namespace LoaderTest
{
    class Program
    {
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern IntPtr LoadLibrary(string lpFileName);

        static void Main(string[] args)
        {
            string path = args.Length > 0 ? args[0] : "NativeMedia.dll";
            Console.WriteLine($"Trying to load {path}...");
            
            // Try explicit load
            IntPtr ptr = LoadLibrary(path);
            if (ptr == IntPtr.Zero)
            {
                int err = Marshal.GetLastWin32Error();
                Console.WriteLine($"Failed to load. Error: {err}");
                
                string absPath = System.IO.Path.GetFullPath(path);
                Console.WriteLine($"Trying absolute path: {absPath}");
                ptr = LoadLibrary(absPath);
                if (ptr == IntPtr.Zero)
                {
                    err = Marshal.GetLastWin32Error();
                    Console.WriteLine($"Failed to load absolute path. Error: {err}");
                }
                else
                {
                    Console.WriteLine("Success with absolute path!");
                }
            }
            else
            {
                Console.WriteLine("Success!");
            }
        }
    }
}
