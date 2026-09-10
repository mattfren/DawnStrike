if (-not ("Dawnstrike.Native.JobProcessRunner" -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.Native
{
    public sealed class JobProcessResult
    {
        public string Stdout { get; set; }
        public string Stderr { get; set; }
        public int ExitCode { get; set; }
        public int ActiveJobMembersAfterCleanup { get; set; }
        public ulong JobMemoryLimitBytes { get; set; }
        public ulong JobMemoryLimitReadbackBytes { get; set; }
        public uint JobLimitFlags { get; set; }
        public ulong ProcessTreeRssLimitBytes { get; set; }
        public ulong PeakJobMemoryUsedBytes { get; set; }
        public ulong LastJobMemoryUsedBytes { get; set; }
        public ulong PeakProcessTreeRssBytes { get; set; }
        public ulong LastProcessTreeRssBytes { get; set; }
        public int ProcessTreeRssSamples { get; set; }
        public bool ProcessTreeRssMeasurementAvailable { get; set; }
        public string GuardFailure { get; set; }
    }

    public static class JobProcessRunner
    {
        private const uint CREATE_SUSPENDED = 0x00000004;
        private const uint CREATE_NO_WINDOW = 0x08000000;
        private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
        private const uint STARTF_USESTDHANDLES = 0x00000100;
        private const uint HANDLE_FLAG_INHERIT = 0x00000001;
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        private const uint JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200;
        private const int JobObjectBasicAccountingInformation = 1;
        private const int JobObjectBasicProcessIdList = 3;
        private const int JobObjectExtendedLimitInformation = 9;
        private const uint WAIT_OBJECT_0 = 0x00000000;
        private const uint WAIT_TIMEOUT = 0x00000102;
        private const uint WAIT_FAILED = 0xFFFFFFFF;
        private const uint JOB_TERMINATION_EXIT_CODE = 0xD15EA5ED;
        private const uint PROCESS_QUERY_INFORMATION = 0x00000400;
        private const uint PROCESS_VM_READ = 0x00000010;
        private const uint ERROR_MORE_DATA = 234;

        [StructLayout(LayoutKind.Sequential)]
        private struct SECURITY_ATTRIBUTES
        {
            public int nLength;
            public IntPtr lpSecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)] public bool bInheritHandle;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO
        {
            public int cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public int dwX;
            public int dwY;
            public int dwXSize;
            public int dwYSize;
            public int dwXCountChars;
            public int dwYCountChars;
            public int dwFillAttribute;
            public int dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public uint dwProcessId;
            public uint dwThreadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        {
            public long TotalUserTime;
            public long TotalKernelTime;
            public long ThisPeriodTotalUserTime;
            public long ThisPeriodTotalKernelTime;
            public uint TotalPageFaultCount;
            public uint TotalProcesses;
            public uint ActiveProcesses;
            public uint TotalTerminatedProcesses;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_MEMORY_COUNTERS
        {
            public uint cb;
            public uint PageFaultCount;
            public UIntPtr PeakWorkingSetSize;
            public UIntPtr WorkingSetSize;
            public UIntPtr QuotaPeakPagedPoolUsage;
            public UIntPtr QuotaPagedPoolUsage;
            public UIntPtr QuotaPeakNonPagedPoolUsage;
            public UIntPtr QuotaNonPagedPoolUsage;
            public UIntPtr PagefileUsage;
            public UIntPtr PeakPagefileUsage;
        }

        private sealed class GuardState
        {
            public readonly object Sync = new object();
            public string Failure;
            public ulong PeakJobMemoryUsedBytes;
            public ulong LastJobMemoryUsedBytes;
            public ulong PeakProcessTreeRssBytes;
            public ulong LastProcessTreeRssBytes;
            public int ProcessTreeRssSamples;
            public bool ProcessTreeRssMeasurementAvailable;
            public bool JobTerminatedByGuard;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(
            IntPtr lpJobAttributes,
            string lpName
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetInformationJobObject(
            IntPtr hJob,
            int JobObjectInfoClass,
            IntPtr lpJobObjectInfo,
            uint cbJobObjectInfoLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool QueryInformationJobObject(
            IntPtr hJob,
            int JobObjectInfoClass,
            IntPtr lpJobObjectInfo,
            uint cbJobObjectInfoLength,
            IntPtr lpReturnLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AssignProcessToJobObject(
            IntPtr hJob,
            IntPtr hProcess
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateJobObject(IntPtr hJob, uint uExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreatePipe(
            out IntPtr hReadPipe,
            out IntPtr hWritePipe,
            ref SECURITY_ATTRIBUTES lpPipeAttributes,
            uint nSize
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetHandleInformation(
            IntPtr hObject,
            uint dwMask,
            uint dwFlags
        );

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateProcessW(
            string lpApplicationName,
            StringBuilder lpCommandLine,
            IntPtr lpProcessAttributes,
            IntPtr lpThreadAttributes,
            [MarshalAs(UnmanagedType.Bool)] bool bInheritHandles,
            uint dwCreationFlags,
            IntPtr lpEnvironment,
            string lpCurrentDirectory,
            ref STARTUPINFO lpStartupInfo,
            out PROCESS_INFORMATION lpProcessInformation
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr hThread);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(
            uint dwDesiredAccess,
            [MarshalAs(UnmanagedType.Bool)] bool bInheritHandle,
            uint dwProcessId
        );

        [DllImport("psapi.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetProcessMemoryInfo(
            IntPtr hProcess,
            ref PROCESS_MEMORY_COUNTERS counters,
            uint size
        );

        public static string QuoteArgument(string argument)
        {
            if (argument == null)
            {
                throw new ArgumentNullException("argument");
            }
            bool needsQuotes = argument.Length == 0;
            for (int index = 0; index < argument.Length && !needsQuotes; index += 1)
            {
                needsQuotes = char.IsWhiteSpace(argument[index]) || argument[index] == '"';
            }
            if (!needsQuotes)
            {
                return argument;
            }

            StringBuilder encoded = new StringBuilder();
            encoded.Append('"');
            int backslashes = 0;
            foreach (char value in argument)
            {
                if (value == '\\')
                {
                    backslashes += 1;
                    continue;
                }
                if (value == '"')
                {
                    encoded.Append('\\', backslashes * 2 + 1);
                    encoded.Append('"');
                    backslashes = 0;
                    continue;
                }
                encoded.Append('\\', backslashes);
                encoded.Append(value);
                backslashes = 0;
            }
            encoded.Append('\\', backslashes * 2);
            encoded.Append('"');
            return encoded.ToString();
        }

        public static JobProcessResult Run(
            string filePath,
            string[] arguments,
            string workingDirectory,
            string label,
            int timeoutMilliseconds,
            int outputDrainMilliseconds,
            string[] environmentOverrides,
            ulong jobMemoryLimitBytes,
            ulong processTreeRssLimitBytes,
            int rssSampleMilliseconds
        )
        {
            if (String.IsNullOrWhiteSpace(filePath))
            {
                throw new ArgumentException("filePath is required.");
            }
            if (String.IsNullOrWhiteSpace(workingDirectory))
            {
                throw new ArgumentException("workingDirectory is required.");
            }
            if (timeoutMilliseconds < 1 || outputDrainMilliseconds < 1)
            {
                throw new ArgumentOutOfRangeException("timeouts must be positive.");
            }
            if (jobMemoryLimitBytes < 1 || processTreeRssLimitBytes < 1 || rssSampleMilliseconds < 1)
            {
                throw new ArgumentOutOfRangeException("guard limits must be positive.");
            }

            IntPtr job = IntPtr.Zero;
            IntPtr stdoutRead = IntPtr.Zero;
            IntPtr stdoutWrite = IntPtr.Zero;
            IntPtr stderrRead = IntPtr.Zero;
            IntPtr stderrWrite = IntPtr.Zero;
            IntPtr stdinRead = IntPtr.Zero;
            IntPtr stdinWrite = IntPtr.Zero;
            IntPtr environment = IntPtr.Zero;
            PROCESS_INFORMATION processInfo = new PROCESS_INFORMATION();
            bool assignedToJob = false;
            bool cleanupConfirmed = false;
            int activeAfterCleanup = -1;
            StreamReader stdoutReader = null;
            StreamReader stderrReader = null;
            Task<string> stdoutTask = null;
            Task<string> stderrTask = null;
            CancellationTokenSource guardStop = null;
            Task guardTask = null;
            GuardState guard = new GuardState();
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION configuredLimits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();

            try
            {
                job = CreateJobObject(IntPtr.Zero, null);
                RequireHandle(job, "CreateJobObject");
                configuredLimits = ConfigureJobLimits(job, jobMemoryLimitBytes);

                SECURITY_ATTRIBUTES security = new SECURITY_ATTRIBUTES();
                security.nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
                security.bInheritHandle = true;
                Require(CreatePipe(out stdoutRead, out stdoutWrite, ref security, 0), "CreatePipe(stdout)");
                Require(CreatePipe(out stderrRead, out stderrWrite, ref security, 0), "CreatePipe(stderr)");
                Require(CreatePipe(out stdinRead, out stdinWrite, ref security, 0), "CreatePipe(stdin)");
                Require(SetHandleInformation(stdoutRead, HANDLE_FLAG_INHERIT, 0), "SetHandleInformation(stdout)");
                Require(SetHandleInformation(stderrRead, HANDLE_FLAG_INHERIT, 0), "SetHandleInformation(stderr)");
                Require(SetHandleInformation(stdinWrite, HANDLE_FLAG_INHERIT, 0), "SetHandleInformation(stdin)");

                STARTUPINFO startup = new STARTUPINFO();
                startup.cb = Marshal.SizeOf(typeof(STARTUPINFO));
                startup.dwFlags = (int)STARTF_USESTDHANDLES;
                startup.hStdInput = stdinRead;
                startup.hStdOutput = stdoutWrite;
                startup.hStdError = stderrWrite;

                environment = BuildEnvironmentBlock(environmentOverrides);
                StringBuilder commandLine = BuildCommandLine(filePath, arguments);
                uint flags = CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT;
                Require(
                    CreateProcessW(
                        filePath,
                        commandLine,
                        IntPtr.Zero,
                        IntPtr.Zero,
                        true,
                        flags,
                        environment,
                        workingDirectory,
                        ref startup,
                        out processInfo
                    ),
                    "CreateProcessW"
                );
                Require(AssignProcessToJobObject(job, processInfo.hProcess), "AssignProcessToJobObject");
                assignedToJob = true;
                CloseOwnedHandle(ref stdoutWrite);
                CloseOwnedHandle(ref stderrWrite);
                CloseOwnedHandle(ref stdinRead);
                CloseOwnedHandle(ref stdinWrite);

                stdoutReader = ReaderFor(ref stdoutRead);
                stderrReader = ReaderFor(ref stderrRead);
                stdoutTask = stdoutReader.ReadToEndAsync();
                stderrTask = stderrReader.ReadToEndAsync();

                if (ResumeThread(processInfo.hThread) == UInt32.MaxValue)
                {
                    throw NativeFailure("ResumeThread");
                }
                CloseOwnedHandle(ref processInfo.hThread);

                guardStop = new CancellationTokenSource();
                guardTask = Task.Run(() => MonitorJob(job, guardStop.Token, guard, processTreeRssLimitBytes, rssSampleMilliseconds));
                DateTime deadline = DateTime.UtcNow.AddMilliseconds(timeoutMilliseconds);
                while (true)
                {
                    string guardFailure = GetGuardFailure(guard);
                    if (guardFailure != null)
                    {
                        throw new InvalidOperationException(guardFailure);
                    }
                    int remaining = (int)Math.Max(1, (deadline - DateTime.UtcNow).TotalMilliseconds);
                    uint wait = WaitForSingleObject(processInfo.hProcess, (uint)Math.Min(100, remaining));
                    if (wait == WAIT_OBJECT_0)
                    {
                        break;
                    }
                    if (wait == WAIT_FAILED)
                    {
                        throw NativeFailure("WaitForSingleObject");
                    }
                    if (DateTime.UtcNow >= deadline)
                    {
                        throw new InvalidOperationException(
                            String.Format(
                                "{0} timed out after {1} milliseconds.",
                                label,
                                timeoutMilliseconds
                            )
                        );
                    }
                }
                string completedGuardFailure = GetGuardFailure(guard);
                if (completedGuardFailure != null)
                {
                    throw new InvalidOperationException(completedGuardFailure);
                }

                uint rawExitCode;
                Require(GetExitCodeProcess(processInfo.hProcess, out rawExitCode), "GetExitCodeProcess");
                Task[] outputTasks = new Task[] { stdoutTask, stderrTask };
                if (!Task.WaitAll(outputTasks, outputDrainMilliseconds))
                {
                    throw new InvalidOperationException(label + " output drain timed out after root exit.");
                }

                string stdout = stdoutTask.Result.Trim();
                string stderr = stderrTask.Result.Trim();
                if (rawExitCode != 0)
                {
                    activeAfterCleanup = TerminateOwnedJob(
                        job,
                        processInfo.hProcess,
                        true,
                        outputDrainMilliseconds
                    );
                    cleanupConfirmed = true;
                }
                else
                {
                    activeAfterCleanup = WaitForJobEmpty(job, 1000);
                    if (activeAfterCleanup != 0)
                    {
                        throw new InvalidOperationException(
                            label + " left owned descendants after root exit."
                        );
                    }
                    cleanupConfirmed = true;
                }

                return new JobProcessResult
                {
                    Stdout = stdout,
                    Stderr = stderr,
                    ExitCode = unchecked((int)rawExitCode),
                    ActiveJobMembersAfterCleanup = activeAfterCleanup,
                    JobMemoryLimitBytes = jobMemoryLimitBytes,
                    JobMemoryLimitReadbackBytes = configuredLimits.JobMemoryLimit.ToUInt64(),
                    JobLimitFlags = configuredLimits.BasicLimitInformation.LimitFlags,
                    ProcessTreeRssLimitBytes = processTreeRssLimitBytes,
                    PeakJobMemoryUsedBytes = guard.PeakJobMemoryUsedBytes,
                    LastJobMemoryUsedBytes = guard.LastJobMemoryUsedBytes,
                    PeakProcessTreeRssBytes = guard.PeakProcessTreeRssBytes,
                    LastProcessTreeRssBytes = guard.LastProcessTreeRssBytes,
                    ProcessTreeRssSamples = guard.ProcessTreeRssSamples,
                    ProcessTreeRssMeasurementAvailable = guard.ProcessTreeRssMeasurementAvailable,
                    GuardFailure = guard.Failure
                };
            }
            catch (Exception failure)
            {
                Exception cleanupFailure = null;
                if (guardStop != null)
                {
                    guardStop.Cancel();
                    if (guardTask != null)
                    {
                        try { guardTask.Wait(2000); } catch { }
                    }
                }
                if (!cleanupConfirmed)
                {
                    try
                    {
                        bool alreadyTerminated = false;
                        lock (guard.Sync) { alreadyTerminated = guard.JobTerminatedByGuard; }
                        activeAfterCleanup = alreadyTerminated
                            ? WaitForOwnedJobTermination(job, processInfo.hProcess, outputDrainMilliseconds)
                            : TerminateOwnedJob(
                                job,
                                processInfo.hProcess,
                                assignedToJob,
                                outputDrainMilliseconds
                            );
                        cleanupConfirmed = true;
                    }
                    catch (Exception cleanup)
                    {
                        cleanupFailure = cleanup;
                    }
                }
                string cleanupDetail = cleanupFailure == null
                    ? String.Empty
                    : " cleanup_failure=" + cleanupFailure.Message + ".";
                throw new InvalidOperationException(
                    String.Format(
                        "{0} active_job_members_after_cleanup={1}.{2}",
                        failure.Message,
                        activeAfterCleanup,
                        cleanupDetail
                    ),
                    cleanupFailure == null
                        ? failure
                        : new AggregateException(failure, cleanupFailure)
                );
            }
            finally
            {
                if (guardStop != null)
                {
                    guardStop.Cancel();
                    if (guardTask != null)
                    {
                        try { guardTask.Wait(2000); } catch { }
                    }
                }
                // Close the kill-on-close job first. Even if a later stream
                // disposal were to fail, no owned process can escape this
                // outermost native lifetime boundary.
                CloseOwnedHandle(ref job);
                CloseOwnedHandle(ref processInfo.hThread);
                CloseOwnedHandle(ref processInfo.hProcess);
                CloseOwnedHandle(ref stdoutWrite);
                CloseOwnedHandle(ref stderrWrite);
                CloseOwnedHandle(ref stdinRead);
                CloseOwnedHandle(ref stdinWrite);
                CloseOwnedHandle(ref stdoutRead);
                CloseOwnedHandle(ref stderrRead);
                if (stdoutReader != null) stdoutReader.Dispose();
                if (stderrReader != null) stderrReader.Dispose();
                if (environment != IntPtr.Zero) Marshal.FreeHGlobal(environment);
            }
        }

        private static StringBuilder BuildCommandLine(string filePath, string[] arguments)
        {
            StringBuilder commandLine = new StringBuilder(QuoteArgument(filePath));
            foreach (string argument in arguments ?? new string[0])
            {
                commandLine.Append(' ');
                commandLine.Append(QuoteArgument(argument));
            }
            return commandLine;
        }

        private static IntPtr BuildEnvironmentBlock(string[] overrides)
        {
            SortedDictionary<string, string> values = new SortedDictionary<string, string>(
                StringComparer.OrdinalIgnoreCase
            );
            foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
            {
                values[Convert.ToString(entry.Key)] = Convert.ToString(entry.Value);
            }
            foreach (string pair in overrides ?? new string[0])
            {
                int separator = pair == null ? -1 : pair.IndexOf('=');
                if (separator <= 0)
                {
                    throw new ArgumentException("Environment override must be NAME=VALUE.");
                }
                values[pair.Substring(0, separator)] = pair.Substring(separator + 1);
            }
            StringBuilder block = new StringBuilder();
            foreach (KeyValuePair<string, string> entry in values)
            {
                block.Append(entry.Key);
                block.Append('=');
                block.Append(entry.Value);
                block.Append('\0');
            }
            block.Append('\0');
            return Marshal.StringToHGlobalUni(block.ToString());
        }

        private static StreamReader ReaderFor(ref IntPtr handle)
        {
            SafeFileHandle safe = new SafeFileHandle(handle, true);
            handle = IntPtr.Zero;
            FileStream stream = new FileStream(safe, FileAccess.Read, 4096, false);
            return new StreamReader(stream, Encoding.UTF8, true, 4096, false);
        }

        private static JOBOBJECT_EXTENDED_LIMIT_INFORMATION ConfigureJobLimits(IntPtr job, ulong jobMemoryLimitBytes)
        {
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits =
                new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags =
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_JOB_MEMORY;
            limits.JobMemoryLimit = (UIntPtr)jobMemoryLimitBytes;
            int size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr pointer = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(limits, pointer, false);
                Require(
                    SetInformationJobObject(
                        job,
                        JobObjectExtendedLimitInformation,
                        pointer,
                        (uint)size
                    ),
                    "SetInformationJobObject"
                );
            }
            finally
            {
                Marshal.FreeHGlobal(pointer);
            }
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION readback = QueryExtendedLimits(job);
            uint flags = readback.BasicLimitInformation.LimitFlags;
            if ((flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) == 0
                || (flags & JOB_OBJECT_LIMIT_JOB_MEMORY) == 0
                || readback.JobMemoryLimit.ToUInt64() != jobMemoryLimitBytes)
            {
                throw new InvalidOperationException(
                    String.Format(
                        "Job Object memory admission readback mismatch: requested={0}, readback={1}, flags=0x{2:X8}.",
                        jobMemoryLimitBytes,
                        readback.JobMemoryLimit.ToUInt64(),
                        flags
                    )
                );
            }
            return readback;
        }

        private static JOBOBJECT_EXTENDED_LIMIT_INFORMATION QueryExtendedLimits(IntPtr job)
        {
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits =
                new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            int size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr pointer = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(limits, pointer, false);
                Require(
                    QueryInformationJobObject(
                        job,
                        JobObjectExtendedLimitInformation,
                        pointer,
                        (uint)size,
                        IntPtr.Zero
                    ),
                    "QueryInformationJobObject(extended limits)"
                );
                return (JOBOBJECT_EXTENDED_LIMIT_INFORMATION)Marshal.PtrToStructure(
                    pointer,
                    typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
                );
            }
            finally
            {
                Marshal.FreeHGlobal(pointer);
            }
        }

        private static string GetGuardFailure(GuardState guard)
        {
            lock (guard.Sync)
            {
                return guard.Failure;
            }
        }

        private static void SetGuardFailure(
            IntPtr job,
            GuardState guard,
            string failure
        )
        {
            bool terminate = false;
            lock (guard.Sync)
            {
                if (guard.Failure == null)
                {
                    guard.Failure = failure;
                    terminate = true;
                }
            }
            if (terminate && job != IntPtr.Zero)
            {
                try
                {
                    TerminateJobObject(job, JOB_TERMINATION_EXIT_CODE);
                    lock (guard.Sync) { guard.JobTerminatedByGuard = true; }
                }
                catch { }
            }
        }

        private static void MonitorJob(
            IntPtr job,
            CancellationToken cancellation,
            GuardState guard,
            ulong rssLimitBytes,
            int sampleMilliseconds
        )
        {
            while (!cancellation.IsCancellationRequested)
            {
                int active;
                try
                {
                    active = QueryActiveProcesses(job);
                }
                catch (Exception failure)
                {
                    SetGuardFailure(job, guard, "process-tree RSS measurement unavailable: " + failure.Message);
                    return;
                }
                if (active == 0)
                {
                    return;
                }
                try
                {
                    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = QueryExtendedLimits(job);
                    ulong jobMemory = limits.PeakJobMemoryUsed.ToUInt64();
                    lock (guard.Sync)
                    {
                        guard.LastJobMemoryUsedBytes = jobMemory;
                        if (jobMemory > guard.PeakJobMemoryUsedBytes)
                        {
                            guard.PeakJobMemoryUsedBytes = jobMemory;
                        }
                    }
                }
                catch (Exception failure)
                {
                    SetGuardFailure(job, guard, "Job Object memory readback unavailable: " + failure.Message);
                    return;
                }
                ulong rss;
                string unavailable;
                if (!TryGetJobProcessTreeRss(job, out rss, out unavailable))
                {
                    SetGuardFailure(
                        job,
                        guard,
                        "process-tree RSS measurement unavailable: " + unavailable
                    );
                    return;
                }
                lock (guard.Sync)
                {
                    guard.ProcessTreeRssMeasurementAvailable = true;
                    guard.ProcessTreeRssSamples += 1;
                    guard.LastProcessTreeRssBytes = rss;
                    if (rss > guard.PeakProcessTreeRssBytes)
                    {
                        guard.PeakProcessTreeRssBytes = rss;
                    }
                }
                if (rss > rssLimitBytes)
                {
                    SetGuardFailure(
                        job,
                        guard,
                        String.Format(
                            "process-tree RSS cap exceeded: observed={0}, limit={1}, samples={2}.",
                            rss,
                            rssLimitBytes,
                            guard.ProcessTreeRssSamples
                        )
                    );
                    return;
                }
                try
                {
                    Thread.Sleep(sampleMilliseconds);
                }
                catch (ThreadInterruptedException)
                {
                    return;
                }
            }
        }

        private static bool TryGetJobProcessTreeRss(
            IntPtr job,
            out ulong rss,
            out string failure
        )
        {
            rss = 0;
            failure = null;
            uint[] processIds;
            if (!TryGetJobProcessIds(job, out processIds, out failure))
            {
                return false;
            }
            foreach (uint processId in processIds)
            {
                IntPtr process = OpenProcess(
                    PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                    false,
                    processId
                );
                if (process == IntPtr.Zero)
                {
                    failure = "OpenProcess failed for an owned process.";
                    return false;
                }
                try
                {
                    PROCESS_MEMORY_COUNTERS counters = new PROCESS_MEMORY_COUNTERS();
                    counters.cb = (uint)Marshal.SizeOf(typeof(PROCESS_MEMORY_COUNTERS));
                    if (!GetProcessMemoryInfo(
                        process,
                        ref counters,
                        (uint)Marshal.SizeOf(typeof(PROCESS_MEMORY_COUNTERS))
                    ))
                    {
                        failure = "GetProcessMemoryInfo failed for an owned process.";
                        return false;
                    }
                    rss = checked(rss + counters.WorkingSetSize.ToUInt64());
                }
                finally
                {
                    CloseHandle(process);
                }
            }
            return true;
        }

        private static bool TryGetJobProcessIds(
            IntPtr job,
            out uint[] processIds,
            out string failure
        )
        {
            processIds = new uint[0];
            failure = null;
            int capacity = 32;
            for (int attempt = 0; attempt < 8; attempt++)
            {
                int headerSize = sizeof(uint) * 2;
                int size = checked(headerSize + IntPtr.Size * capacity);
                IntPtr pointer = Marshal.AllocHGlobal(size);
                try
                {
                    if (!QueryInformationJobObject(
                        job,
                        JobObjectBasicProcessIdList,
                        pointer,
                        (uint)size,
                        IntPtr.Zero
                    ))
                    {
                        int error = Marshal.GetLastWin32Error();
                        if (error == ERROR_MORE_DATA)
                        {
                            capacity = checked(capacity * 2);
                            continue;
                        }
                        failure = "QueryInformationJobObject(process list) failed with Win32 error " + error;
                        return false;
                    }
                    uint count = unchecked((uint)Marshal.ReadInt32(pointer, sizeof(uint)));
                    if (count > 4096)
                    {
                        failure = "Job process list exceeded the bounded supervisor capacity.";
                        return false;
                    }
                    if (count > capacity)
                    {
                        capacity = checked((int)count);
                        continue;
                    }
                    processIds = new uint[count];
                    for (int index = 0; index < count; index++)
                    {
                        long value = Marshal.ReadIntPtr(pointer, headerSize + index * IntPtr.Size).ToInt64();
                        if (value <= 0 || value > UInt32.MaxValue)
                        {
                            failure = "Job process list contained an invalid process identity.";
                            return false;
                        }
                        processIds[index] = checked((uint)value);
                    }
                    return true;
                }
                finally
                {
                    Marshal.FreeHGlobal(pointer);
                }
            }
            failure = "Job process list exceeded the bounded supervisor capacity.";
            return false;
        }

        private static int TerminateOwnedJob(
            IntPtr job,
            IntPtr process,
            bool assignedToJob,
            int waitMilliseconds
        )
        {
            if (assignedToJob && job != IntPtr.Zero)
            {
                Require(
                    TerminateJobObject(job, JOB_TERMINATION_EXIT_CODE),
                    "TerminateJobObject"
                );
            }
            if (!assignedToJob && process != IntPtr.Zero)
            {
                uint rootState = WaitForSingleObject(process, 0);
                if (rootState == WAIT_TIMEOUT)
                {
                    // The retained process handle is identity-bound. The root
                    // is still suspended here, so it cannot have descendants.
                    Require(
                        TerminateProcess(process, JOB_TERMINATION_EXIT_CODE),
                        "TerminateProcess(unassigned suspended root)"
                    );
                }
                else if (rootState == WAIT_FAILED)
                {
                    throw NativeFailure("WaitForSingleObject(cleanup preflight)");
                }
            }
            if (process != IntPtr.Zero)
            {
                uint rootWait = WaitForSingleObject(
                    process,
                    (uint)Math.Max(waitMilliseconds, 1000)
                );
                if (rootWait == WAIT_FAILED)
                {
                    throw NativeFailure("WaitForSingleObject(cleanup)");
                }
                if (rootWait != WAIT_OBJECT_0)
                {
                    throw new InvalidOperationException(
                        "Owned root did not terminate during bounded cleanup."
                    );
                }
            }
            int active = job == IntPtr.Zero
                ? 0
                : WaitForJobEmpty(job, Math.Max(waitMilliseconds, 5000));
            if (active != 0)
            {
                throw new InvalidOperationException(
                    String.Format(
                        "Owned job retained {0} active members after termination.",
                        active
                    )
                );
            }
            return active;
        }

        private static int WaitForOwnedJobTermination(
            IntPtr job,
            IntPtr process,
            int waitMilliseconds
        )
        {
            if (process != IntPtr.Zero)
            {
                uint rootWait = WaitForSingleObject(
                    process,
                    (uint)Math.Max(waitMilliseconds, 1000)
                );
                if (rootWait == WAIT_FAILED)
                {
                    throw NativeFailure("WaitForSingleObject(guard cleanup)");
                }
                if (rootWait != WAIT_OBJECT_0)
                {
                    throw new InvalidOperationException(
                        "Guard-terminated root did not exit during bounded cleanup."
                    );
                }
            }
            int active = job == IntPtr.Zero
                ? 0
                : WaitForJobEmpty(job, Math.Max(waitMilliseconds, 5000));
            if (active != 0)
            {
                throw new InvalidOperationException(
                    String.Format(
                        "Guard-terminated job retained {0} active members after cleanup.",
                        active
                    )
                );
            }
            return active;
        }

        private static int WaitForJobEmpty(IntPtr job, int timeoutMilliseconds)
        {
            DateTime deadline = DateTime.UtcNow.AddMilliseconds(timeoutMilliseconds);
            int active = QueryActiveProcesses(job);
            while (active != 0 && DateTime.UtcNow < deadline)
            {
                Thread.Sleep(25);
                active = QueryActiveProcesses(job);
            }
            return active;
        }

        private static int QueryActiveProcesses(IntPtr job)
        {
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting =
                new JOBOBJECT_BASIC_ACCOUNTING_INFORMATION();
            int size = Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION));
            IntPtr pointer = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(accounting, pointer, false);
                Require(
                    QueryInformationJobObject(
                        job,
                        JobObjectBasicAccountingInformation,
                        pointer,
                        (uint)size,
                        IntPtr.Zero
                    ),
                    "QueryInformationJobObject"
                );
                accounting = (JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
                    Marshal.PtrToStructure(
                        pointer,
                        typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
                    );
                return checked((int)accounting.ActiveProcesses);
            }
            finally
            {
                Marshal.FreeHGlobal(pointer);
            }
        }

        private static void Require(bool condition, string operation)
        {
            if (!condition) throw NativeFailure(operation);
        }

        private static void RequireHandle(IntPtr handle, string operation)
        {
            if (handle == IntPtr.Zero || handle == new IntPtr(-1))
            {
                throw NativeFailure(operation);
            }
        }

        private static Exception NativeFailure(string operation)
        {
            return new Win32Exception(Marshal.GetLastWin32Error(), operation + " failed");
        }

        private static void CloseOwnedHandle(ref IntPtr handle)
        {
            if (handle != IntPtr.Zero && handle != new IntPtr(-1))
            {
                CloseHandle(handle);
            }
            handle = IntPtr.Zero;
        }
    }
}
'@
}

function Invoke-DawnstrikeJobProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][ValidateRange(1, 86400)][int]$TimeoutSeconds,
        [Parameter()][ValidateRange(1, 60)][int]$OutputDrainTimeoutSeconds = 5,
        [Parameter()][hashtable]$EnvironmentOverrides = @{},
        [Parameter()][ValidateRange(1, 268435456)][UInt64]$JobMemoryLimitBytes = 268435456,
        [Parameter()][ValidateRange(1, 268435456)][UInt64]$ProcessTreeRssLimitBytes = 268435456,
        [Parameter()][ValidateRange(1, 10000)][int]$RssSampleMilliseconds = 100
    )

    $environmentPairs = @(
        $EnvironmentOverrides.GetEnumerator() |
            Sort-Object -Property Key |
            ForEach-Object { "{0}={1}" -f $_.Key, $_.Value }
    )
    return [Dawnstrike.Native.JobProcessRunner]::Run(
        $FilePath,
        @($ArgumentList),
        $WorkingDirectory,
        $Label,
        $TimeoutSeconds * 1000,
        $OutputDrainTimeoutSeconds * 1000,
        $environmentPairs,
        $JobMemoryLimitBytes,
        $ProcessTreeRssLimitBytes,
        $RssSampleMilliseconds
    )
}
