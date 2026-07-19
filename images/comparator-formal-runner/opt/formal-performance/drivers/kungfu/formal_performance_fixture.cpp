// SPDX-License-Identifier: Apache-2.0

#include <kungfu/runtime/durable_ingest.h>
#include <kungfu/yijinjing/ownership.h>

#include <nlohmann/json.hpp>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace fs = std::filesystem;
using json = nlohmann::json;
using namespace kungfu::runtime::durability;
using kungfu::yijinjing::ownership::lease;

namespace {

using clock_type = std::chrono::steady_clock;
constexpr const char *RESULT_SCHEMA =
    "urn:kungfu-systems:build-images:formal-performance-driver-result:v1";

uint64_t elapsed_ns(clock_type::time_point start,
                    clock_type::time_point end = clock_type::now()) {
  return static_cast<uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count());
}

uint64_t parse_u64(const char *value, const std::string &name,
                   uint64_t minimum = 0) {
  try {
    const auto parsed = std::stoull(value);
    if (parsed < minimum) {
      throw std::invalid_argument(name + " is below its minimum");
    }
    return parsed;
  } catch (const std::exception &) {
    throw std::invalid_argument("invalid " + name + ": " + value);
  }
}

class histogram {
public:
  void observe(uint64_t value) {
    const auto index = bucket_index(value);
    ++counts_[index];
    ++count_;
    maximum_ = std::max(maximum_, value);
  }

  [[nodiscard]] uint64_t count() const { return count_; }

  [[nodiscard]] json render() const {
    return {{"p50_ns", percentile(0.5)},
            {"p95_ns", percentile(0.95)},
            {"p99_ns", percentile(0.99)},
            {"p999_ns", percentile(0.999)},
            {"max_ns", maximum_}};
  }

private:
  static constexpr uint64_t SUB_BUCKETS = 32;

  static uint64_t bucket_index(uint64_t value) {
    if (value <= 1) {
      return 0;
    }
    const auto exponent = static_cast<uint64_t>(
        std::floor(std::log2(static_cast<long double>(value))));
    const auto base = std::ldexp(1.0L, static_cast<int>(exponent));
    const auto fraction = (static_cast<long double>(value) - base) / base;
    const auto sub = std::min<uint64_t>(
        SUB_BUCKETS - 1, static_cast<uint64_t>(fraction * SUB_BUCKETS));
    return exponent * SUB_BUCKETS + sub + 1;
  }

  static uint64_t bucket_upper(uint64_t index) {
    if (index == 0) {
      return 1;
    }
    const auto adjusted = index - 1;
    const auto exponent = adjusted / SUB_BUCKETS;
    const auto sub = adjusted % SUB_BUCKETS;
    const auto base = std::ldexp(1.0L, static_cast<int>(exponent));
    const auto upper =
        base * (1.0L + static_cast<long double>(sub + 1) / SUB_BUCKETS);
    return upper >=
                   static_cast<long double>(std::numeric_limits<uint64_t>::max())
               ? std::numeric_limits<uint64_t>::max()
               : static_cast<uint64_t>(std::ceil(upper));
  }

  [[nodiscard]] uint64_t percentile(double quantile) const {
    if (count_ == 0) {
      return 0;
    }
    const auto target =
        static_cast<uint64_t>(std::ceil(static_cast<double>(count_) * quantile));
    uint64_t seen = 0;
    for (const auto &[index, count] : counts_) {
      seen += count;
      if (seen >= target) {
        return bucket_upper(index);
      }
    }
    return maximum_;
  }

  std::map<uint64_t, uint64_t> counts_;
  uint64_t count_ = 0;
  uint64_t maximum_ = 0;
};

stream_position position(uint64_t sequence) {
  return {71, 5, sequence, 1000 + sequence};
}

ingest_options options(const fs::path &root, bool read_only = false) {
  ingest_options result{root.string(),
                        71,
                        5,
                        "00000001.00000002",
                        "candidate/formal-performance-linux-x64-v1",
                        true,
                        64ULL * 1024ULL * 1024ULL,
                        read_only};
  result.activation = ingest_activation::ProductionCandidate;
  return result;
}

uint64_t tree_bytes(const fs::path &root) {
  uint64_t result = 0;
  for (const auto &entry : fs::recursive_directory_iterator(root)) {
    if (entry.is_regular_file()) {
      result += entry.file_size();
    }
  }
  return result;
}

void copy_tree_new(const fs::path &source, const fs::path &target) {
  if (fs::exists(target)) {
    throw std::runtime_error("refusing existing copy target: " +
                             target.string());
  }
  fs::create_directories(target);
  for (const auto &entry : fs::recursive_directory_iterator(source)) {
    const auto destination = target / fs::relative(entry.path(), source);
    if (entry.is_directory()) {
      fs::create_directories(destination);
    } else if (entry.is_regular_file()) {
      fs::create_directories(destination.parent_path());
      fs::copy_file(entry.path(), destination, fs::copy_options::none);
    } else {
      throw std::runtime_error("unsupported data-root entry");
    }
  }
}

durability_profile durable_profile(const std::string &mode) {
  if (mode == "durable_group") {
    return durability_profile::DurableGroup;
  }
  if (mode == "durable_sync") {
    return durability_profile::DurableSync;
  }
  throw std::invalid_argument("mode has no durable profile: " + mode);
}

json run(const fs::path &root, const std::string &mode,
         const std::string &workload, uint64_t records, uint64_t payload_bytes,
         uint64_t group_max_messages, uint64_t group_max_millis,
         uint64_t duration_seconds) {
  if (fs::exists(root)) {
    throw std::runtime_error("refusing existing run root: " + root.string());
  }
  if (mode != "visible" && mode != "durable_group" &&
      mode != "durable_sync") {
    throw std::invalid_argument("unsupported mode");
  }
  if (workload != "latency" && workload != "throughput" &&
      workload != "soak" && workload != "recovery") {
    throw std::invalid_argument("unsupported workload");
  }
  if ((records == 0) == (duration_seconds == 0)) {
    throw std::invalid_argument(
        "exactly one of records or duration seconds must be nonzero");
  }
  if (payload_bytes < 64 || (group_max_messages != 100) ||
      (group_max_millis != 10)) {
    throw std::invalid_argument("payload or durable_group policy drifted");
  }

  fs::create_directories(root);
  const std::string marker = "formal-performance-kungfu-v1";
  std::string payload(payload_bytes, 'k');
  std::copy(marker.begin(), marker.end(), payload.begin());
  histogram receipt_latency;
  std::vector<clock_type::time_point> pending;
  pending.reserve(group_max_messages);
  uint64_t completed = 0;
  uint64_t request_id = 10000;
  const auto started = clock_type::now();
  const auto deadline =
      duration_seconds == 0
          ? clock_type::time_point::max()
          : started + std::chrono::seconds(duration_seconds);
  auto receipt_finished = started;

  {
    const auto service_owner =
        lease::acquire_data_root_service(root.string());
    const auto writer_owner =
        lease::acquire_stream_writer(root.string(), "00000001.00000002");
    durable_ingest_log log(options(root));
    auto group_started = clock_type::now();
    while ((records > 0 && completed < records) ||
           (duration_seconds > 0 && clock_type::now() < deadline)) {
      const auto append_started = clock_type::now();
      log.append(position(completed + 1), 9001, payload, service_owner,
                 writer_owner);
      ++completed;
      if (mode == "visible") {
        receipt_latency.observe(elapsed_ns(append_started));
      } else if (mode == "durable_sync") {
        const auto barrier = log.barrier(++request_id, durable_profile(mode),
                                         service_owner, writer_owner);
        if (barrier.receipt.status != receipt_status::Succeeded ||
            !barrier.receipt.durable_watermark.has_value() ||
            barrier.receipt.durable_watermark->sequence != completed) {
          throw std::runtime_error("durable_sync receipt failed");
        }
        receipt_latency.observe(elapsed_ns(append_started));
      } else {
        if (pending.empty()) {
          group_started = append_started;
        }
        pending.push_back(append_started);
        if (pending.size() == group_max_messages ||
            elapsed_ns(group_started) >= group_max_millis * 1000000ULL) {
          const auto barrier = log.barrier(++request_id, durable_profile(mode),
                                           service_owner, writer_owner);
          if (barrier.receipt.status != receipt_status::Succeeded ||
              !barrier.receipt.durable_watermark.has_value() ||
              barrier.receipt.durable_watermark->sequence != completed) {
            throw std::runtime_error("durable_group receipt failed");
          }
          const auto receipt_at = clock_type::now();
          for (const auto appended_at : pending) {
            receipt_latency.observe(elapsed_ns(appended_at, receipt_at));
          }
          pending.clear();
        }
      }
    }
    if (!pending.empty()) {
      const auto barrier = log.barrier(++request_id, durable_profile(mode),
                                       service_owner, writer_owner);
      if (barrier.receipt.status != receipt_status::Succeeded ||
          !barrier.receipt.durable_watermark.has_value() ||
          barrier.receipt.durable_watermark->sequence != completed) {
        throw std::runtime_error("final durable_group receipt failed");
      }
      const auto receipt_at = clock_type::now();
      for (const auto appended_at : pending) {
        receipt_latency.observe(elapsed_ns(appended_at, receipt_at));
      }
    }
    receipt_finished = clock_type::now();
    if (mode == "visible") {
      const auto final_barrier =
          log.barrier(++request_id, durability_profile::DurableGroup,
                      service_owner, writer_owner);
      if (final_barrier.receipt.status != receipt_status::Succeeded ||
          !final_barrier.receipt.durable_watermark.has_value() ||
          final_barrier.receipt.durable_watermark->sequence != completed) {
        throw std::runtime_error(
            "visible run final recovery checkpoint failed");
      }
    }
  }
  if (receipt_latency.count() != completed) {
    throw std::runtime_error("receipt histogram count is incomplete");
  }

  auto verify_recovered = [&](const auto &recovered_records) {
    uint64_t marker_mismatches = 0;
    uint64_t reordered = 0;
    for (uint64_t index = 0; index < recovered_records.size(); ++index) {
      const auto &record = recovered_records[index];
      if (record.position.sequence != index + 1) {
        ++reordered;
      }
      if (record.payload.size() != payload.size() ||
          !std::equal(marker.begin(), marker.end(), record.payload.begin())) {
        ++marker_mismatches;
      }
    }
    const auto loss =
        completed >= recovered_records.size()
            ? completed - static_cast<uint64_t>(recovered_records.size())
            : 0;
    const auto duplicates =
        recovered_records.size() > completed
            ? static_cast<uint64_t>(recovered_records.size()) - completed
            : 0;
    if (loss != 0 || duplicates != 0 || reordered != 0 ||
        marker_mismatches != 0) {
      throw std::runtime_error("recovery sequence or marker oracle failed");
    }
  };

  const auto crash_replay_started = clock_type::now();
  durable_ingest_log crash_recovered(options(root, true));
  verify_recovered(crash_recovered.read_durable_records());
  const auto crash_replay_ns = elapsed_ns(crash_replay_started);

  uint64_t whole_root_restore_ns = 0;
  if (workload == "recovery") {
    const auto whole_root_restore_started = clock_type::now();
    const auto backup_root =
        root.parent_path() / (root.filename().string() + ".backup");
    const auto restore_root =
        root.parent_path() / (root.filename().string() + ".restore");
    copy_tree_new(root, backup_root);
    copy_tree_new(backup_root, restore_root);
    durable_ingest_log restored(options(restore_root, true));
    verify_recovered(restored.read_durable_records());
    whole_root_restore_ns = elapsed_ns(whole_root_restore_started);
  }
  const auto recovery_ns = crash_replay_ns + whole_root_restore_ns;

  const auto receipt_ns = elapsed_ns(started, receipt_finished);
  const auto messages_per_second =
      static_cast<double>(completed) * 1e9 /
      static_cast<double>(std::max<uint64_t>(1, receipt_ns));
  auto latency = receipt_latency.render();
  return {{"schema", RESULT_SCHEMA},
          {"product", "kungfu"},
          {"mode", mode},
          {"workload", workload},
          {"payload_bytes", payload_bytes},
          {"messages", completed},
          {"messages_per_second", messages_per_second},
          {"bytes_per_second", messages_per_second * payload_bytes},
          {"p50_ns", latency["p50_ns"]},
          {"p95_ns", latency["p95_ns"]},
          {"p99_ns", latency["p99_ns"]},
          {"p999_ns", latency["p999_ns"]},
          {"max_ns", latency["max_ns"]},
          {"recovery_ns", recovery_ns},
          {"crash_replay_ns", crash_replay_ns},
          {"whole_root_restore_ns", whole_root_restore_ns},
          {"data_root_bytes", tree_bytes(root)},
          {"backpressure", 0},
          {"durable_group",
           {{"max_messages", group_max_messages},
            {"max_millis", group_max_millis}}},
          {"anomalies",
           {{"loss", 0},
            {"duplicates", 0},
            {"reordered", 0},
            {"marker_mismatches", 0}}}};
}

void usage() {
  std::cerr
      << "usage: formal_performance_fixture run ROOT MODE WORKLOAD RECORDS "
         "PAYLOAD_BYTES GROUP_MAX_MESSAGES GROUP_MAX_MILLIS DURATION_SECONDS\n";
}

} // namespace

int main(int argc, char **argv) {
  try {
    if (argc != 10 || std::string(argv[1]) != "run") {
      usage();
      return 2;
    }
    std::cout
        << run(fs::path(argv[2]), argv[3], argv[4],
               parse_u64(argv[5], "records"), parse_u64(argv[6], "payload", 64),
               parse_u64(argv[7], "group max messages", 1),
               parse_u64(argv[8], "group max millis", 1),
               parse_u64(argv[9], "duration seconds"))
               .dump()
        << std::endl;
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "formal_performance_fixture: " << error.what() << std::endl;
    return 1;
  }
}
