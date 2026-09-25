// Precompiled Dawnstrike support surface formerly embedded in scripts/state_root_boundary.ps1.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.StateBoundary {
    [StructLayout(LayoutKind.Sequential)]
    internal struct ByHandleFileInformation {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    internal static class NativeMethods {
        internal const uint GENERIC_READ = 0x80000000;
        internal const uint OPEN_EXISTING = 3;
        internal const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
        internal const uint FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000;
        internal const uint FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400;
        internal const uint MOVEFILE_REPLACE_EXISTING = 0x00000001;
        internal const uint MOVEFILE_WRITE_THROUGH = 0x00000008;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern SafeFileHandle CreateFileW(
            string fileName,
            uint desiredAccess,
            FileShare shareMode,
            IntPtr securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetFileInformationByHandle(
            SafeFileHandle handle,
            out ByHandleFileInformation information);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool MoveFileExW(
            string existingFileName,
            string newFileName,
            uint flags);
    }

    public sealed class BoundPath : IDisposable {
        public SafeFileHandle Handle { get; private set; }
        public string Identity { get; private set; }
        public uint Attributes { get; private set; }

        private BoundPath(SafeFileHandle handle, string identity, uint attributes) {
            Handle = handle;
            Identity = identity;
            Attributes = attributes;
        }

        public static BoundPath Open(string path) {
            SafeFileHandle handle = NativeMethods.CreateFileW(
                path,
                // A metadata-only READ_CONTROL handle does not prevent a
                // modern Windows directory rename. GENERIC_READ combined
                // with a share mask that omits Delete does, while remaining
                // available to the admitted read/execute principal.
                NativeMethods.GENERIC_READ,
                FileShare.Read | FileShare.Write,
                IntPtr.Zero,
                NativeMethods.OPEN_EXISTING,
                NativeMethods.FILE_FLAG_BACKUP_SEMANTICS |
                    NativeMethods.FILE_FLAG_OPEN_REPARSE_POINT,
                IntPtr.Zero);
            if (handle.IsInvalid) {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error, "Could not bind StateRoot path: " + path);
            }
            ByHandleFileInformation information;
            if (!NativeMethods.GetFileInformationByHandle(handle, out information)) {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error, "Could not identify StateRoot path: " + path);
            }
            if ((information.FileAttributes & NativeMethods.FILE_ATTRIBUTE_REPARSE_POINT) != 0) {
                handle.Dispose();
                throw new InvalidOperationException("StateRoot boundary contains a reparse point: " + path);
            }
            string identity = information.VolumeSerialNumber.ToString("x8") + ":" +
                information.FileIndexHigh.ToString("x8") +
                information.FileIndexLow.ToString("x8");
            return new BoundPath(handle, identity, information.FileAttributes);
        }

        public void Dispose() {
            if (Handle != null) {
                Handle.Dispose();
                Handle = null;
            }
        }
    }

    public sealed class BoundPathChain : IDisposable {
        private List<BoundPath> paths;
        public string Identity { get; private set; }
        public uint Attributes { get; private set; }

        private BoundPathChain(List<BoundPath> paths) {
            this.paths = paths;
            BoundPath terminal = paths[paths.Count - 1];
            Identity = terminal.Identity;
            Attributes = terminal.Attributes;
        }

        public static BoundPathChain Open(string path) {
            string full = Path.GetFullPath(path);
            string root = Path.GetPathRoot(full);
            if (String.IsNullOrEmpty(root)) {
                throw new InvalidOperationException("StateRoot boundary path has no trusted volume anchor.");
            }
            List<BoundPath> opened = new List<BoundPath>();
            try {
                string cursor = root;
                opened.Add(BoundPath.Open(cursor));
                string tail = full.Substring(root.Length);
                string[] components = tail.Split(
                    new char[] { Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar },
                    StringSplitOptions.RemoveEmptyEntries);
                foreach (string component in components) {
                    cursor = Path.Combine(cursor, component);
                    opened.Add(BoundPath.Open(cursor));
                }
                return new BoundPathChain(opened);
            }
            catch {
                for (int index = opened.Count - 1; index >= 0; index--) {
                    opened[index].Dispose();
                }
                throw;
            }
        }

        public void Dispose() {
            if (paths == null) { return; }
            for (int index = paths.Count - 1; index >= 0; index--) {
                paths[index].Dispose();
            }
            paths = null;
        }
    }

    public sealed class DisposableGroup : IDisposable {
        private IDisposable[] items;
        public DisposableGroup(IDisposable[] items) { this.items = items; }
        public void Dispose() {
            if (items == null) { return; }
            for (int index = items.Length - 1; index >= 0; index--) {
                if (items[index] != null) { items[index].Dispose(); }
            }
            items = null;
        }
    }

    public static class AtomicFile {
        public static void Replace(string source, string destination) {
            if (!NativeMethods.MoveFileExW(
                    source,
                    destination,
                    NativeMethods.MOVEFILE_REPLACE_EXISTING |
                        NativeMethods.MOVEFILE_WRITE_THROUGH)) {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Could not atomically replace protected StateRoot evidence: " + destination);
            }
        }

        public static void MoveNoReplace(string source, string destination) {
            if (!NativeMethods.MoveFileExW(
                    source,
                    destination,
                    NativeMethods.MOVEFILE_WRITE_THROUGH)) {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Could not atomically create protected StateRoot evidence: " + destination);
            }
        }
    }
}
