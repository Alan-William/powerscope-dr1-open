// Volatile 32-bit device access for an explicitly verified UIO map.
// Mapping/size/device identity checks live in ps/axi.py. No /dev/mem access.
#include <cstdint>
#include <atomic>
extern "C" void ps_mmio_barrier(){
#if defined(__aarch64__) || defined(__arm__)
    asm volatile("dmb sy" ::: "memory");
#else
    std::atomic_thread_fence(std::memory_order_seq_cst);
#endif
}
extern "C" uint32_t ps_mmio_read(void* base,uint32_t offset){
    auto p=reinterpret_cast<volatile uint32_t*>(static_cast<uint8_t*>(base)+offset);
    uint32_t value=*p;ps_mmio_barrier();return value;
}
extern "C" void ps_mmio_write(void* base,uint32_t offset,uint32_t value){
    auto p=reinterpret_cast<volatile uint32_t*>(static_cast<uint8_t*>(base)+offset);
    *p=value;ps_mmio_barrier();
}
