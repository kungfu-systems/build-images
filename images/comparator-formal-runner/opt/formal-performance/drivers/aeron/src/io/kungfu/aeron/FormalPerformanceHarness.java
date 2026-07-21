package io.kungfu.aeron;

import io.aeron.Aeron;
import io.aeron.ChannelUri;
import io.aeron.ExclusivePublication;
import io.aeron.Subscription;
import io.aeron.archive.client.AeronArchive;
import io.aeron.archive.codecs.SourceLocation;
import io.aeron.archive.status.RecordingPos;
import io.aeron.logbuffer.FragmentHandler;
import io.aeron.shadow.org.HdrHistogram.Histogram;
import org.agrona.DirectBuffer;
import org.agrona.collections.MutableLong;
import org.agrona.concurrent.UnsafeBuffer;
import org.agrona.concurrent.status.CountersReader;

import java.nio.ByteBuffer;
import java.nio.file.StandardCopyOption;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;

public final class FormalPerformanceHarness
{
    private static final String SCHEMA =
        "urn:kungfu-systems:build-images:formal-performance-driver-result:v1";
    private static final String IPC_CHANNEL = "aeron:ipc";
    private static final String CONTROL_REQUEST = "aeron:udp?endpoint=localhost:8010";
    private static final String CONTROL_RESPONSE = "aeron:udp?endpoint=localhost:0";
    private static final int RECORDING_STREAM = 3101;
    private static final int REPLAY_STREAM = 3102;
    private static final int MARKER_OFFSET = 16;
    private static final byte[] MARKER = "formal-perf-v1!!".getBytes();
    private static final long TIMEOUT_NS = TimeUnit.SECONDS.toNanos(30);
    private static final long ARCHIVE_CONTROL_POLL_INTERVAL_NS =
        TimeUnit.MILLISECONDS.toNanos(500);

    private FormalPerformanceHarness()
    {
    }

    public static void main(final String[] args) throws Exception
    {
        if (args.length == 0 || !"run".equals(args[0]))
        {
            fail("run command is required");
        }
        final Map<String, String> options = parseOptions(args);
        final Path root = requiredPath(options, "root").toAbsolutePath();
        final String mode = required(options, "mode");
        final String workload = required(options, "workload");
        final int payload = positive(options, "payload");
        final int records = nonNegative(options, "records");
        final int durationSeconds = nonNegative(options, "duration-seconds");
        final int soakMessagesPerSecond =
            nonNegative(options, "soak-messages-per-second");
        final int groupMaxMessages = positive(options, "group-max-messages");
        final int groupMaxMillis = positive(options, "group-max-millis");
        if (!mode.equals("visible") && !mode.equals("durable_group") &&
            !mode.equals("durable_sync"))
        {
            fail("mode is unsupported");
        }
        if (!workload.equals("latency") && !workload.equals("throughput") &&
            !workload.equals("soak") && !workload.equals("recovery"))
        {
            fail("workload is unsupported");
        }
        if (payload < MARKER_OFFSET + MARKER.length)
        {
            fail("payload is too small for the fixed sequence/timestamp/marker fields");
        }
        if ((records == 0) == (durationSeconds == 0))
        {
            fail("exactly one of records or duration-seconds must be nonzero");
        }
        if ((workload.equals("soak") && soakMessagesPerSecond != 10000) ||
            (!workload.equals("soak") && soakMessagesPerSecond != 0))
        {
            fail("soak rate contract drifted");
        }
        if (groupMaxMessages != 100 || groupMaxMillis != 10)
        {
            fail("durable_group policy must be 100 messages or 10 ms");
        }
        System.out.println(run(
            root, mode, workload, payload, records, durationSeconds,
            soakMessagesPerSecond, groupMaxMessages, groupMaxMillis));
    }

    private static String run(
        final Path root,
        final String mode,
        final String workload,
        final int payload,
        final int records,
        final int durationSeconds,
        final int soakMessagesPerSecond,
        final int groupMaxMessages,
        final int groupMaxMillis) throws Exception
    {
        final String driverDir = root.resolve("driver").toString();
        final UnsafeBuffer buffer =
            new UnsafeBuffer(ByteBuffer.allocateDirect(payload));
        final MutableLong expected = new MutableLong();
        final MutableLong observed = new MutableLong();
        final MutableLong duplicates = new MutableLong();
        final MutableLong reordered = new MutableLong();
        final MutableLong markerMismatches = new MutableLong();
        final Histogram receiptLatency =
            new Histogram(TimeUnit.SECONDS.toNanos(60), 3);
        final long[] pending = new long[groupMaxMessages];
        final long[] backpressure = new long[1];
        long recordingId;
        long finalPosition;
        long sent = 0;
        long groupStartedNs = 0;
        int pendingCount = 0;
        final long startedNs = System.nanoTime();
        final long deadlineNs = durationSeconds == 0 ?
            Long.MAX_VALUE : startedNs + TimeUnit.SECONDS.toNanos(durationSeconds);
        long nextArchiveControlPollNs =
            startedNs + ARCHIVE_CONTROL_POLL_INTERVAL_NS;

        final FragmentHandler handler = (data, offset, length, header) ->
        {
            final long sequence = data.getLong(offset);
            if (sequence < expected.get())
            {
                duplicates.increment();
            }
            else if (sequence != expected.get())
            {
                reordered.increment();
                expected.set(sequence + 1);
            }
            else
            {
                expected.increment();
            }
            observed.increment();
            if (!markerMatches(data, offset + MARKER_OFFSET))
            {
                markerMismatches.increment();
            }
            if (mode.equals("visible"))
            {
                receiptLatency.recordValue(
                    Math.max(1, System.nanoTime() - data.getLong(offset + 8)));
            }
        };

        try (Aeron aeron = Aeron.connect(
                new Aeron.Context().aeronDirectoryName(driverDir));
            AeronArchive archive = AeronArchive.connect(archiveContext(aeron));
            ExclusivePublication publication =
                aeron.addExclusivePublication(IPC_CHANNEL, RECORDING_STREAM))
        {
            final String sessionChannel =
                ChannelUri.addSessionId(IPC_CHANNEL, publication.sessionId());
            final long subscriptionId = archive.startRecording(
                sessionChannel, RECORDING_STREAM, SourceLocation.LOCAL);
            try (Subscription subscription =
                    aeron.addSubscription(sessionChannel, RECORDING_STREAM))
            {
                await(
                    () -> publication.isConnected() && subscription.isConnected(),
                    "publication/subscription connection");
                final CountersReader counters = aeron.countersReader();
                final int counterId = awaitRecordingCounter(
                    counters, publication.sessionId(), archive.archiveId());
                recordingId = RecordingPos.getRecordingId(counters, counterId);

                while ((records > 0 && sent < records) ||
                       (durationSeconds > 0 && System.nanoTime() < deadlineNs))
                {
                    final long sequence = sent;
                    final long sentNs = System.nanoTime();
                    buffer.putLong(0, sequence);
                    buffer.putLong(8, sentNs);
                    buffer.putBytes(MARKER_OFFSET, MARKER);
                    final long offerDeadline = sentNs + TIMEOUT_NS;
                    while (publication.offer(buffer, 0, payload) < 0)
                    {
                        backpressure[0]++;
                        subscription.poll(handler, 64);
                        if (System.nanoTime() > offerDeadline)
                        {
                            fail("publication offer timed out");
                        }
                        Thread.onSpinWait();
                    }
                    sent++;
                    while (observed.get() < sent)
                    {
                        subscription.poll(handler, 64);
                        if (System.nanoTime() > offerDeadline)
                        {
                            fail("visibility receipt timed out");
                        }
                        Thread.onSpinWait();
                    }

                    if (mode.equals("durable_sync"))
                    {
                        awaitCounter(counters, counterId, publication.position());
                        receiptLatency.recordValue(
                            Math.max(1, System.nanoTime() - sentNs));
                    }
                    else if (mode.equals("durable_group"))
                    {
                        if (pendingCount == 0)
                        {
                            groupStartedNs = sentNs;
                        }
                        pending[pendingCount++] = sentNs;
                        final long now = System.nanoTime();
                        if (pendingCount == groupMaxMessages ||
                            now - groupStartedNs >=
                                TimeUnit.MILLISECONDS.toNanos(groupMaxMillis))
                        {
                            awaitCounter(counters, counterId, publication.position());
                            final long receiptNs = System.nanoTime();
                            for (int index = 0; index < pendingCount; index++)
                            {
                                receiptLatency.recordValue(
                                    Math.max(1, receiptNs - pending[index]));
                            }
                            pendingCount = 0;
                        }
                    }
                    final long nowNs = System.nanoTime();
                    if (nowNs >= nextArchiveControlPollNs)
                    {
                        archive.checkForErrorResponse();
                        nextArchiveControlPollNs =
                            nowNs + ARCHIVE_CONTROL_POLL_INTERVAL_NS;
                    }
                    if (soakMessagesPerSecond > 0)
                    {
                        final long targetNs = startedNs +
                            sent * TimeUnit.SECONDS.toNanos(1) /
                                soakMessagesPerSecond;
                        long remainingNs;
                        while ((remainingNs = targetNs - System.nanoTime()) > 0)
                        {
                            LockSupport.parkNanos(remainingNs);
                        }
                    }
                }
                if (pendingCount > 0)
                {
                    awaitCounter(counters, counterId, publication.position());
                    final long receiptNs = System.nanoTime();
                    for (int index = 0; index < pendingCount; index++)
                    {
                        receiptLatency.recordValue(
                            Math.max(1, receiptNs - pending[index]));
                    }
                }
                finalPosition = publication.position();
                awaitCounter(counters, counterId, finalPosition);
                if (mode.equals("visible") &&
                    receiptLatency.getTotalCount() != sent)
                {
                    fail("visible receipt histogram count is incomplete");
                }
            }
            finally
            {
                archive.stopRecording(subscriptionId);
            }
        }
        final long receiptFinishedNs = System.nanoTime();
        final long recoveryStartedNs = System.nanoTime();
        final MutableLong replayed = new MutableLong();
        final MutableLong replayExpected = new MutableLong();
        final MutableLong replayDuplicates = new MutableLong();
        final MutableLong replayReordered = new MutableLong();
        final MutableLong replayMarkerMismatches = new MutableLong();
        final FragmentHandler replayHandler = (data, offset, length, header) ->
        {
            final long sequence = data.getLong(offset);
            if (sequence < replayExpected.get())
            {
                replayDuplicates.increment();
            }
            else if (sequence != replayExpected.get())
            {
                replayReordered.increment();
                replayExpected.set(sequence + 1);
            }
            else
            {
                replayExpected.increment();
            }
            replayed.increment();
            if (!markerMatches(data, offset + MARKER_OFFSET))
            {
                replayMarkerMismatches.increment();
            }
        };
        try (Aeron aeron = Aeron.connect(
                new Aeron.Context().aeronDirectoryName(driverDir));
            AeronArchive archive = AeronArchive.connect(archiveContext(aeron));
            Subscription replay = archive.replay(
                recordingId, 0, finalPosition, IPC_CHANNEL, REPLAY_STREAM))
        {
            await(replay::isConnected, "replay connection");
            final long replayDeadline = System.nanoTime() + TIMEOUT_NS;
            while (replayed.get() < sent)
            {
                replay.poll(replayHandler, 128);
                if (System.nanoTime() > replayDeadline)
                {
                    fail("replay timed out");
                }
                Thread.onSpinWait();
            }
        }
        final long crashReplayNs = System.nanoTime() - recoveryStartedNs;
        final long wholeRootRestoreNs =
            wholeRootRestore(root, workload);
        final long recoveryNs = crashReplayNs + wholeRootRestoreNs;
        final long loss = sent - observed.get();
        if (loss != 0 || duplicates.get() != 0 || reordered.get() != 0 ||
            markerMismatches.get() != 0 || replayed.get() != sent ||
            replayDuplicates.get() != 0 || replayReordered.get() != 0 ||
            replayMarkerMismatches.get() != 0)
        {
            fail("sequence, marker, or recovery oracle failed");
        }
        final long receiptDurationNs = receiptFinishedNs - startedNs;
        final double messagesPerSecond =
            sent * 1_000_000_000.0 / Math.max(1, receiptDurationNs);
        final long dataRootBytes = treeBytes(root);
        return String.format(
            "{\"schema\":\"%s\",\"product\":\"aeron\",\"mode\":\"%s\"," +
            "\"workload\":\"%s\",\"payload_bytes\":%d,\"messages\":%d," +
            "\"messages_per_second\":%.12f,\"bytes_per_second\":%.12f," +
            "\"p50_ns\":%d,\"p95_ns\":%d,\"p99_ns\":%d,\"p999_ns\":%d," +
            "\"max_ns\":%d,\"recovery_ns\":%d,\"crash_replay_ns\":%d," +
            "\"whole_root_restore_ns\":%d,\"data_root_bytes\":%d," +
            "\"backpressure\":%d,\"durable_group\":{\"max_messages\":%d," +
            "\"max_millis\":%d},\"anomalies\":{\"loss\":0,\"duplicates\":0," +
            "\"reordered\":0,\"marker_mismatches\":0}}",
            SCHEMA, mode, workload, payload, sent, messagesPerSecond,
            messagesPerSecond * payload,
            receiptLatency.getValueAtPercentile(50),
            receiptLatency.getValueAtPercentile(95),
            receiptLatency.getValueAtPercentile(99),
            receiptLatency.getValueAtPercentile(99.9),
            receiptLatency.getMaxValue(), recoveryNs, crashReplayNs,
            wholeRootRestoreNs, dataRootBytes,
            backpressure[0], groupMaxMessages, groupMaxMillis);
    }

    private static long wholeRootRestore(
        final Path root, final String workload) throws Exception
    {
        if (!workload.equals("recovery"))
        {
            return 0;
        }
        final Path source = root.resolve("archive");
        final Path target = root.resolve("whole-root-restore/archive");
        if (!Files.isDirectory(source) || Files.exists(target))
        {
            fail("fresh Aeron archive restore roots are required");
        }
        final long startedNs = System.nanoTime();
        try (var paths = Files.walk(source))
        {
            for (final Path path : paths.toList())
            {
                final Path relative = source.relativize(path);
                if (relative.toString().contains("archive-mark.dat") ||
                    relative.toString().contains("loss-report.dat"))
                {
                    continue;
                }
                final Path destination = target.resolve(relative);
                if (Files.isDirectory(path))
                {
                    Files.createDirectories(destination);
                }
                else if (Files.isRegularFile(path))
                {
                    Files.createDirectories(destination.getParent());
                    Files.copy(path, destination, StandardCopyOption.COPY_ATTRIBUTES);
                    if (Files.size(path) != Files.size(destination) ||
                        Files.mismatch(path, destination) != -1)
                    {
                        fail("Aeron whole-root restore verification failed");
                    }
                }
            }
        }
        return System.nanoTime() - startedNs;
    }

    private static long treeBytes(final Path root) throws Exception
    {
        try (var paths = Files.walk(root))
        {
            return paths.filter(Files::isRegularFile).mapToLong(path ->
            {
                try
                {
                    return Files.size(path);
                }
                catch (final Exception error)
                {
                    throw new IllegalStateException(error);
                }
            }).sum();
        }
    }

    private static int awaitRecordingCounter(
        final CountersReader counters, final int sessionId, final long archiveId)
    {
        final long deadline = System.nanoTime() + TIMEOUT_NS;
        int counterId;
        while (Aeron.NULL_VALUE ==
            (counterId = RecordingPos.findCounterIdBySession(
                counters, sessionId, archiveId)))
        {
            if (System.nanoTime() > deadline)
            {
                fail("recording counter timed out");
            }
            Thread.onSpinWait();
        }
        return counterId;
    }

    private static void awaitCounter(
        final CountersReader counters, final int counterId, final long position)
    {
        final long deadline = System.nanoTime() + TIMEOUT_NS;
        while (counters.getCounterValue(counterId) < position)
        {
            if (System.nanoTime() > deadline)
            {
                fail("archive recording position timed out");
            }
            Thread.onSpinWait();
        }
    }

    private static AeronArchive.Context archiveContext(final Aeron aeron)
    {
        return new AeronArchive.Context()
            .aeron(aeron)
            .controlRequestChannel(CONTROL_REQUEST)
            .controlResponseChannel(CONTROL_RESPONSE);
    }

    private static boolean markerMatches(final DirectBuffer data, final int offset)
    {
        if (data.capacity() < offset + MARKER.length)
        {
            return false;
        }
        for (int index = 0; index < MARKER.length; index++)
        {
            if (data.getByte(offset + index) != MARKER[index])
            {
                return false;
            }
        }
        return true;
    }

    private static Map<String, String> parseOptions(final String[] args)
    {
        final Map<String, String> options = new HashMap<>();
        for (int index = 1; index < args.length; index += 2)
        {
            if (index + 1 >= args.length || !args[index].startsWith("--"))
            {
                fail("options must be --name value pairs");
            }
            options.put(args[index].substring(2), args[index + 1]);
        }
        return options;
    }

    private static String required(
        final Map<String, String> options, final String name)
    {
        final String value = options.get(name);
        if (value == null || value.isBlank())
        {
            fail("--" + name + " is required");
        }
        return value;
    }

    private static Path requiredPath(
        final Map<String, String> options, final String name)
    {
        return Path.of(required(options, name));
    }

    private static int positive(
        final Map<String, String> options, final String name)
    {
        final int value = Integer.parseInt(required(options, name));
        if (value <= 0)
        {
            fail("--" + name + " must be positive");
        }
        return value;
    }

    private static int nonNegative(
        final Map<String, String> options, final String name)
    {
        final int value = Integer.parseInt(required(options, name));
        if (value < 0)
        {
            fail("--" + name + " must be non-negative");
        }
        return value;
    }

    private static void await(
        final BooleanSupplier condition, final String context)
    {
        final long deadline = System.nanoTime() + TIMEOUT_NS;
        while (!condition.get())
        {
            if (System.nanoTime() > deadline)
            {
                fail(context + " timed out");
            }
            Thread.onSpinWait();
        }
    }

    private static void fail(final String message)
    {
        throw new IllegalStateException(message);
    }

    @FunctionalInterface
    private interface BooleanSupplier
    {
        boolean get();
    }
}
