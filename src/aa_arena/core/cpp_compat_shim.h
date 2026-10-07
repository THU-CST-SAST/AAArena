// Compatibility shim for historical GNU C++ submissions on C++17 toolchains.
#pragma once

#include <algorithm>
#include <iterator>
#include <random>

#if !defined(AGENTBENCH_COMPAT_RANDOM_SHUFFLE) && (__cplusplus >= 201703L)
#define AGENTBENCH_COMPAT_RANDOM_SHUFFLE
namespace std {
template <class RandomIt>
inline void random_shuffle(RandomIt first, RandomIt last) {
    static thread_local std::mt19937 generator{std::random_device{}()};
    typename std::iterator_traits<RandomIt>::difference_type index, count;
    count = last - first;
    for (index = count - 1; index > 0; --index) {
        std::uniform_int_distribution<decltype(index)> distribution(0, index);
        std::swap(first[index], first[distribution(generator)]);
    }
}

template <class RandomIt, class RandomFunc>
inline void random_shuffle(RandomIt first, RandomIt last, RandomFunc&& random_index) {
    typename std::iterator_traits<RandomIt>::difference_type index, count;
    count = last - first;
    for (index = count - 1; index > 0; --index) {
        std::swap(first[index], first[random_index(index + 1)]);
    }
}
}  // namespace std
#endif
